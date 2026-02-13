import os
import secrets
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware

from src.api.db import db
from src.api.models import (
    HuddleCreateRequest,
    HuddleJoinRequest,
    HuddleListResponse,
    HuddleResponse,
    LoginRequest,
    MarkNotificationReadRequest,
    NotificationResponse,
    NotificationsListResponse,
    ParticipantResponse,
    ParticipantsListResponse,
    SignupRequest,
    TokenResponse,
    UpdateProfileRequest,
    UserProfile,
    WsEnvelope,
)
from src.api.security import create_access_token, get_current_user_id, hash_password, verify_password
from src.api.ws_manager import ws_manager

openapi_tags = [
    {"name": "System", "description": "Health and documentation helpers."},
    {"name": "Auth", "description": "Signup/login and JWT issuance."},
    {"name": "Users", "description": "Authenticated user profile."},
    {"name": "Huddles", "description": "Create/list/join/leave huddles and participant listing."},
    {"name": "Notifications", "description": "User notifications."},
    {"name": "WebSockets", "description": "Real-time chat, room events, and WebRTC/screen-share signaling."},
]


def _cors_origins() -> List[str]:
    raw = os.getenv("CORS_ORIGINS", "*").strip()
    if raw == "*":
        return ["*"]
    return [o.strip() for o in raw.split(",") if o.strip()]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _gen_join_code() -> str:
    # 8-10 chars is a good UX; schema allows 6-32.
    return secrets.token_urlsafe(8)[:10]


app = FastAPI(
    title="ConnectHuddle Backend API",
    description=(
        "Backend for ConnectHuddle: JWT auth, huddle management, notifications, and real-time WebSockets "
        "for chat, room events, and WebRTC/screen-share signaling."
    ),
    version="0.3.0",
    openapi_tags=openapi_tags,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup() -> None:
    await db.connect()


@app.on_event("shutdown")
async def _shutdown() -> None:
    await db.disconnect()


# PUBLIC_INTERFACE
@app.get("/", tags=["System"], summary="Health check", description="Returns a simple health status.")
def health_check() -> Dict[str, str]:
    """Health check endpoint used by platform readiness probes."""
    return {"message": "Healthy"}


# PUBLIC_INTERFACE
@app.get(
    "/docs/websockets",
    tags=["System"],
    summary="WebSocket usage",
    description="Explains available WebSocket endpoints and message formats.",
)
def websocket_usage() -> Dict[str, Any]:
    """Return WebSocket connection and payload usage notes for clients."""
    return {
        "endpoints": {
            "chat": "/ws/chat/{huddle_id}",
            "events": "/ws/events/{huddle_id}",
            "signaling": "/ws/signaling/{huddle_id}",
        },
        "auth": "Pass JWT as query param: ?token=... (WebSocket Authorization header is not always convenient in browsers)",
        "message_envelope": {
            "type": "string discriminator (chat_message, participant_joined, webrtc_offer, ...)",
            "huddle_id": "uuid string",
            "sender_user_id": "uuid string (server sets when known)",
            "payload": "object (type-specific)",
        },
        "signaling_types": [
            "webrtc_offer",
            "webrtc_answer",
            "webrtc_ice",
            "screenshare_start",
            "screenshare_stop",
        ],
    }


async def _fetch_user_profile(user_id: str) -> UserProfile:
    row = await db.pool.fetchrow(
        """
        SELECT id::text as id, email, display_name, avatar_url, created_at, updated_at
        FROM users
        WHERE id = $1::uuid AND is_active = true
        """,
        user_id,
    )
    if not row:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found or inactive")
    return UserProfile(**dict(row))


async def _require_huddle_exists(huddle_id: str) -> Dict[str, Any]:
    row = await db.pool.fetchrow(
        """
        SELECT id::text as id, title, description, host_user_id::text as host_user_id,
               is_private, join_code, status, created_at, updated_at, ended_at
        FROM huddles
        WHERE id = $1::uuid
        """,
        huddle_id,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Huddle not found")
    return dict(row)


async def _upsert_participant(huddle_id: str, user_id: str, role: str = "member") -> ParticipantResponse:
    # Ensure single row per user per huddle due to unique index.
    row = await db.pool.fetchrow(
        """
        INSERT INTO huddle_participants (huddle_id, user_id, role, joined_at, left_at)
        VALUES ($1::uuid, $2::uuid, $3, now(), NULL)
        ON CONFLICT (huddle_id, user_id)
        DO UPDATE SET left_at = NULL, role = EXCLUDED.role
        RETURNING id::text as id, huddle_id::text as huddle_id, user_id::text as user_id, role,
                  joined_at, left_at, is_muted, is_video_enabled
        """,
        huddle_id,
        user_id,
        role,
    )
    return ParticipantResponse(**dict(row))


async def _leave_participant(huddle_id: str, user_id: str) -> None:
    await db.pool.execute(
        """
        UPDATE huddle_participants
        SET left_at = now()
        WHERE huddle_id = $1::uuid AND user_id = $2::uuid AND left_at IS NULL
        """,
        huddle_id,
        user_id,
    )


async def _create_notification(
    user_id: str,
    notification_type: str,
    title: Optional[str],
    body: Optional[str],
    huddle_id: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None,
) -> None:
    await db.pool.execute(
        """
        INSERT INTO notifications (user_id, huddle_id, notification_type, title, body, data, is_read)
        VALUES ($1::uuid, $2::uuid, $3, $4, $5, COALESCE($6::jsonb, '{}'::jsonb), false)
        """,
        user_id,
        huddle_id,
        notification_type,
        title,
        body,
        data,
    )


# PUBLIC_INTERFACE
@app.post(
    "/auth/signup",
    tags=["Auth"],
    response_model=TokenResponse,
    summary="Signup",
    description="Create a new user and return a JWT access token.",
)
async def signup(payload: SignupRequest) -> TokenResponse:
    """Create a new user.

    Returns:
        TokenResponse: Bearer JWT token to authenticate future requests.
    """
    existing = await db.pool.fetchrow("SELECT 1 FROM users WHERE lower(email) = lower($1)", str(payload.email))
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")

    pwd_hash = hash_password(payload.password)
    row = await db.pool.fetchrow(
        """
        INSERT INTO users (email, display_name, avatar_url, password_hash, is_active)
        VALUES ($1, $2, $3, $4, true)
        RETURNING id::text as id
        """,
        str(payload.email),
        payload.display_name,
        payload.avatar_url,
        pwd_hash,
    )
    token = create_access_token(row["id"])
    return TokenResponse(access_token=token)


# PUBLIC_INTERFACE
@app.post(
    "/auth/login",
    tags=["Auth"],
    response_model=TokenResponse,
    summary="Login",
    description="Validate credentials and return a JWT access token.",
)
async def login(payload: LoginRequest) -> TokenResponse:
    """Authenticate a user by email/password."""
    row = await db.pool.fetchrow(
        """
        SELECT id::text as id, password_hash, is_active
        FROM users
        WHERE lower(email) = lower($1)
        """,
        str(payload.email),
    )
    if not row or not row["is_active"]:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not row["password_hash"] or not verify_password(payload.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_access_token(row["id"])
    return TokenResponse(access_token=token)


# PUBLIC_INTERFACE
@app.get(
    "/users/me",
    tags=["Users"],
    response_model=UserProfile,
    summary="Get current profile",
    description="Return the authenticated user's profile.",
)
async def get_me(user_id: str = Depends(get_current_user_id)) -> UserProfile:
    """Fetch authenticated user's profile."""
    return await _fetch_user_profile(user_id)


# PUBLIC_INTERFACE
@app.patch(
    "/users/me",
    tags=["Users"],
    response_model=UserProfile,
    summary="Update current profile",
    description="Update display name and/or avatar URL for the authenticated user.",
)
async def update_me(payload: UpdateProfileRequest, user_id: str = Depends(get_current_user_id)) -> UserProfile:
    """Update profile fields for current user."""
    if payload.display_name is None and payload.avatar_url is None:
        return await _fetch_user_profile(user_id)

    await db.pool.execute(
        """
        UPDATE users
        SET display_name = COALESCE($2, display_name),
            avatar_url = COALESCE($3, avatar_url)
        WHERE id = $1::uuid
        """,
        user_id,
        payload.display_name,
        payload.avatar_url,
    )
    return await _fetch_user_profile(user_id)


# PUBLIC_INTERFACE
@app.post(
    "/huddles",
    tags=["Huddles"],
    response_model=HuddleResponse,
    summary="Create huddle",
    description="Create a new huddle. Host is the authenticated user.",
)
async def create_huddle(payload: HuddleCreateRequest, user_id: str = Depends(get_current_user_id)) -> HuddleResponse:
    """Create a new huddle and auto-join the host as a participant."""
    join_code = payload.join_code
    if payload.is_private and not join_code:
        join_code = _gen_join_code()
    if not payload.is_private:
        join_code = None

    row = await db.pool.fetchrow(
        """
        INSERT INTO huddles (title, description, host_user_id, is_private, join_code, status)
        VALUES ($1, $2, $3::uuid, $4, $5, 'active')
        RETURNING id::text as id, title, description, host_user_id::text as host_user_id,
                  is_private, join_code, status, created_at, updated_at, ended_at
        """,
        payload.title,
        payload.description,
        user_id,
        payload.is_private,
        join_code,
    )
    huddle = HuddleResponse(**dict(row))

    await _upsert_participant(huddle.id, user_id, role="host")
    return huddle


# PUBLIC_INTERFACE
@app.get(
    "/huddles",
    tags=["Huddles"],
    response_model=HuddleListResponse,
    summary="List huddles",
    description="List active huddles (optionally include private).",
)
async def list_huddles(
    include_private: bool = Query(False, description="Include private huddles in the response"),
    user_id: str = Depends(get_current_user_id),
) -> HuddleListResponse:
    """List huddles. Private huddles are excluded unless include_private=true."""
    _ = user_id  # reserved for future: personalized feeds
    rows = await db.pool.fetch(
        """
        SELECT id::text as id, title, description, host_user_id::text as host_user_id,
               is_private, join_code, status, created_at, updated_at, ended_at
        FROM huddles
        WHERE status = 'active'
          AND ($1::boolean = true OR is_private = false)
        ORDER BY created_at DESC
        """,
        include_private,
    )
    return HuddleListResponse(items=[HuddleResponse(**dict(r)) for r in rows])


# PUBLIC_INTERFACE
@app.get(
    "/huddles/{huddle_id}",
    tags=["Huddles"],
    response_model=HuddleResponse,
    summary="Get huddle",
    description="Get a single huddle by id.",
)
async def get_huddle(huddle_id: str, user_id: str = Depends(get_current_user_id)) -> HuddleResponse:
    """Fetch huddle info."""
    _ = user_id
    return HuddleResponse(**await _require_huddle_exists(huddle_id))


# PUBLIC_INTERFACE
@app.post(
    "/huddles/{huddle_id}/join",
    tags=["Huddles"],
    response_model=ParticipantResponse,
    summary="Join huddle",
    description="Join a huddle (validates join code for private huddles) and returns participant row.",
)
async def join_huddle(huddle_id: str, payload: HuddleJoinRequest, user_id: str = Depends(get_current_user_id)) -> ParticipantResponse:
    """Join the given huddle."""
    huddle = await _require_huddle_exists(huddle_id)
    if huddle["status"] != "active":
        raise HTTPException(status_code=400, detail="Huddle is not active")

    if huddle["is_private"]:
        if not payload.join_code or payload.join_code != huddle["join_code"]:
            raise HTTPException(status_code=403, detail="Invalid join code")

    participant = await _upsert_participant(huddle_id, user_id, role="member")
    await ws_manager.broadcast(
        huddle_id,
        "events",
        WsEnvelope(type="participant_joined", huddle_id=huddle_id, sender_user_id=user_id, payload={}).model_dump(),
    )
    return participant


# PUBLIC_INTERFACE
@app.post(
    "/huddles/{huddle_id}/leave",
    tags=["Huddles"],
    summary="Leave huddle",
    description="Leave a huddle (marks participant left_at).",
)
async def leave_huddle(huddle_id: str, user_id: str = Depends(get_current_user_id)) -> Dict[str, str]:
    """Leave the huddle."""
    await _require_huddle_exists(huddle_id)
    await _leave_participant(huddle_id, user_id)
    await ws_manager.broadcast(
        huddle_id,
        "events",
        WsEnvelope(type="participant_left", huddle_id=huddle_id, sender_user_id=user_id, payload={}).model_dump(),
    )
    return {"status": "ok"}


# PUBLIC_INTERFACE
@app.get(
    "/huddles/{huddle_id}/participants",
    tags=["Huddles"],
    response_model=ParticipantsListResponse,
    summary="List participants",
    description="List participants for a huddle (including those who left, unless active_only=true).",
)
async def list_participants(
    huddle_id: str,
    active_only: bool = Query(True, description="If true, only participants with left_at IS NULL"),
    user_id: str = Depends(get_current_user_id),
) -> ParticipantsListResponse:
    """List huddle participants."""
    _ = user_id
    await _require_huddle_exists(huddle_id)
    rows = await db.pool.fetch(
        """
        SELECT id::text as id, huddle_id::text as huddle_id, user_id::text as user_id,
               role, joined_at, left_at, is_muted, is_video_enabled
        FROM huddle_participants
        WHERE huddle_id = $1::uuid
          AND ($2::boolean = false OR left_at IS NULL)
        ORDER BY joined_at DESC
        """,
        huddle_id,
        active_only,
    )
    return ParticipantsListResponse(items=[ParticipantResponse(**dict(r)) for r in rows])


# PUBLIC_INTERFACE
@app.get(
    "/notifications",
    tags=["Notifications"],
    response_model=NotificationsListResponse,
    summary="List notifications",
    description="List notifications for the authenticated user. Use unread_only=true to fetch unread items.",
)
async def list_notifications(
    unread_only: bool = Query(False, description="If true, only unread notifications are returned"),
    limit: int = Query(50, ge=1, le=200, description="Max number of notifications"),
    user_id: str = Depends(get_current_user_id),
) -> NotificationsListResponse:
    """List notifications for the current user."""
    rows = await db.pool.fetch(
        """
        SELECT id::text as id, user_id::text as user_id, huddle_id::text as huddle_id,
               notification_type, title, body, data, is_read, created_at, read_at
        FROM notifications
        WHERE user_id = $1::uuid
          AND ($2::boolean = false OR is_read = false)
        ORDER BY created_at DESC
        LIMIT $3
        """,
        user_id,
        unread_only,
        limit,
    )
    return NotificationsListResponse(items=[NotificationResponse(**dict(r)) for r in rows])


# PUBLIC_INTERFACE
@app.patch(
    "/notifications/{notification_id}",
    tags=["Notifications"],
    response_model=NotificationResponse,
    summary="Mark notification read",
    description="Mark a notification read/unread for the authenticated user.",
)
async def mark_notification(
    notification_id: str,
    payload: MarkNotificationReadRequest,
    user_id: str = Depends(get_current_user_id),
) -> NotificationResponse:
    """Update notification read state."""
    row = await db.pool.fetchrow(
        """
        UPDATE notifications
        SET is_read = $3,
            read_at = CASE WHEN $3 = true THEN now() ELSE NULL END
        WHERE id = $1::uuid AND user_id = $2::uuid
        RETURNING id::text as id, user_id::text as user_id, huddle_id::text as huddle_id,
               notification_type, title, body, data, is_read, created_at, read_at
        """,
        notification_id,
        user_id,
        payload.is_read,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Notification not found")
    return NotificationResponse(**dict(row))


async def _ws_auth_user(token: str) -> str:
    # Reuse JWT validation by calling the dependency logic directly.
    # We import decode via get_current_user_id dependency in security module, but it expects Depends.
    from src.api.security import decode_token  # local import to avoid circular

    payload = decode_token(token)
    sub = payload.get("sub")
    if not sub or not isinstance(sub, str):
        raise HTTPException(status_code=401, detail="Invalid token payload")
    # Ensure user exists/active
    await _fetch_user_profile(sub)
    return sub


async def _ensure_user_in_huddle(huddle_id: str, user_id: str) -> None:
    row = await db.pool.fetchrow(
        """
        SELECT 1
        FROM huddle_participants
        WHERE huddle_id = $1::uuid AND user_id = $2::uuid AND left_at IS NULL
        """,
        huddle_id,
        user_id,
    )
    if not row:
        raise HTTPException(status_code=403, detail="Not a participant of this huddle")


# PUBLIC_INTERFACE
@app.websocket("/ws/chat/{huddle_id}")
async def ws_chat(websocket: WebSocket, huddle_id: str, token: str = Query(...)) -> None:
    """WebSocket endpoint for chat messages.

    Query params:
      - token: JWT access token
    Protocol:
      - Client sends WsEnvelope(type='chat_message', payload={'content': '...'})
      - Server persists to chat_messages and broadcasts to room.
    """
    user_id = await _ws_auth_user(token)
    await _ensure_user_in_huddle(huddle_id, user_id)

    await ws_manager.connect(websocket, huddle_id=huddle_id, channel="chat", user_id=user_id)
    await ws_manager.broadcast(
        huddle_id,
        "events",
        WsEnvelope(type="chat_connected", huddle_id=huddle_id, sender_user_id=user_id, payload={}).model_dump(),
    )

    try:
        while True:
            data = await websocket.receive_json()
            env = WsEnvelope(**data)
            if env.huddle_id != huddle_id:
                raise HTTPException(status_code=400, detail="huddle_id mismatch")
            if env.type != "chat_message":
                # allow system passthrough as future enhancement
                continue

            content = str(env.payload.get("content", "")).strip()
            if not content:
                continue

            # Persist
            row = await db.pool.fetchrow(
                """
                INSERT INTO chat_messages (huddle_id, sender_user_id, message_type, content, metadata)
                VALUES ($1::uuid, $2::uuid, 'text', $3, '{}'::jsonb)
                RETURNING id::text as id, created_at
                """,
                huddle_id,
                user_id,
                content,
            )

            outgoing = WsEnvelope(
                type="chat_message",
                huddle_id=huddle_id,
                sender_user_id=user_id,
                payload={"id": row["id"], "content": content, "created_at": row["created_at"].isoformat()},
            ).model_dump()
            await ws_manager.broadcast(huddle_id, "chat", outgoing)

    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket)
    except Exception:
        await ws_manager.disconnect(websocket)


# PUBLIC_INTERFACE
@app.websocket("/ws/events/{huddle_id}")
async def ws_events(websocket: WebSocket, huddle_id: str, token: str = Query(...)) -> None:
    """WebSocket endpoint for room events (participant join/leave, etc.)."""
    user_id = await _ws_auth_user(token)
    await _ensure_user_in_huddle(huddle_id, user_id)
    await ws_manager.connect(websocket, huddle_id=huddle_id, channel="events", user_id=user_id)

    # Notify room and create lightweight notifications for host when someone joins.
    huddle = await _require_huddle_exists(huddle_id)
    await ws_manager.broadcast(
        huddle_id,
        "events",
        WsEnvelope(type="participant_connected", huddle_id=huddle_id, sender_user_id=user_id, payload={}).model_dump(),
    )
    if user_id != huddle["host_user_id"]:
        await _create_notification(
            user_id=huddle["host_user_id"],
            notification_type="participant_joined",
            title="Participant joined",
            body=f"A participant joined huddle '{huddle['title']}'.",
            huddle_id=huddle_id,
            data={"participant_user_id": user_id},
        )

    try:
        while True:
            # Currently server-driven channel; accept keepalives or future client events.
            await websocket.receive_text()
    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket)
        await ws_manager.broadcast(
            huddle_id,
            "events",
            WsEnvelope(type="participant_disconnected", huddle_id=huddle_id, sender_user_id=user_id, payload={}).model_dump(),
        )
    except Exception:
        await ws_manager.disconnect(websocket)


# PUBLIC_INTERFACE
@app.websocket("/ws/signaling/{huddle_id}")
async def ws_signaling(websocket: WebSocket, huddle_id: str, token: str = Query(...)) -> None:
    """WebSocket endpoint for WebRTC and screen-share signaling.

    Supported envelope types:
      - webrtc_offer: payload { target_user_id, sdp }
      - webrtc_answer: payload { target_user_id, sdp }
      - webrtc_ice: payload { target_user_id, candidate }
      - screenshare_start: payload { target_user_id?, stream_id? }
      - screenshare_stop: payload { target_user_id?, stream_id? }

    Notes:
      - Server broadcasts signaling to all participants by default, but includes target_user_id
        so clients can filter (or the server could route in future).
    """
    user_id = await _ws_auth_user(token)
    await _ensure_user_in_huddle(huddle_id, user_id)
    await ws_manager.connect(websocket, huddle_id=huddle_id, channel="signaling", user_id=user_id)

    try:
        while True:
            data = await websocket.receive_json()
            env = WsEnvelope(**data)
            if env.huddle_id != huddle_id:
                raise HTTPException(status_code=400, detail="huddle_id mismatch")

            allowed = {
                "webrtc_offer",
                "webrtc_answer",
                "webrtc_ice",
                "screenshare_start",
                "screenshare_stop",
            }
            if env.type not in allowed:
                continue

            # Basic shape validation
            payload: Dict[str, Any] = dict(env.payload or {})
            payload["from_user_id"] = user_id

            outgoing = WsEnvelope(
                type=env.type,
                huddle_id=huddle_id,
                sender_user_id=user_id,
                payload=payload,
            ).model_dump()
            await ws_manager.broadcast(huddle_id, "signaling", outgoing)

    except WebSocketDisconnect:
        await ws_manager.disconnect(websocket)
    except Exception:
        await ws_manager.disconnect(websocket)
