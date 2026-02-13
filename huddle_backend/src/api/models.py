from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, EmailStr, Field


class TokenResponse(BaseModel):
    access_token: str = Field(..., description="JWT access token")
    token_type: str = Field("bearer", description="Token type, always 'bearer'")


class SignupRequest(BaseModel):
    email: EmailStr = Field(..., description="User email (unique, case-insensitive)")
    password: str = Field(..., min_length=6, description="User password (min 6 chars)")
    display_name: str = Field(..., min_length=1, max_length=80, description="User display name")
    avatar_url: Optional[str] = Field(None, description="Optional avatar URL")


class LoginRequest(BaseModel):
    email: EmailStr = Field(..., description="User email")
    password: str = Field(..., description="User password")


class UserProfile(BaseModel):
    id: str = Field(..., description="User UUID")
    email: EmailStr = Field(..., description="User email")
    display_name: str = Field(..., description="Display name")
    avatar_url: Optional[str] = Field(None, description="Avatar URL")
    created_at: datetime = Field(..., description="Created timestamp")
    updated_at: datetime = Field(..., description="Updated timestamp")


class UpdateProfileRequest(BaseModel):
    display_name: Optional[str] = Field(None, min_length=1, max_length=80, description="New display name")
    avatar_url: Optional[str] = Field(None, description="New avatar URL")


class HuddleCreateRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=120, description="Huddle title")
    description: Optional[str] = Field(None, max_length=500, description="Huddle description")
    is_private: bool = Field(False, description="Whether huddle requires join code")
    join_code: Optional[str] = Field(
        None, min_length=6, max_length=32, description="Join code if private (6-32 chars)"
    )


class HuddleResponse(BaseModel):
    id: str = Field(..., description="Huddle UUID")
    title: str = Field(..., description="Title")
    description: Optional[str] = Field(None, description="Description")
    host_user_id: str = Field(..., description="Host user UUID")
    is_private: bool = Field(..., description="Private flag")
    join_code: Optional[str] = Field(None, description="Join code if private")
    status: str = Field(..., description="active|ended|archived")
    created_at: datetime = Field(..., description="Created timestamp")
    updated_at: datetime = Field(..., description="Updated timestamp")
    ended_at: Optional[datetime] = Field(None, description="Ended timestamp")


class HuddleListResponse(BaseModel):
    items: List[HuddleResponse] = Field(..., description="Huddles list")


class HuddleJoinRequest(BaseModel):
    join_code: Optional[str] = Field(None, description="Join code for private huddle (or null for public)")


class ParticipantResponse(BaseModel):
    id: str = Field(..., description="Participant row UUID")
    huddle_id: str = Field(..., description="Huddle UUID")
    user_id: str = Field(..., description="User UUID")
    role: str = Field(..., description="host|moderator|member")
    joined_at: datetime = Field(..., description="Joined timestamp")
    left_at: Optional[datetime] = Field(None, description="Left timestamp")
    is_muted: bool = Field(..., description="Audio muted")
    is_video_enabled: bool = Field(..., description="Video enabled")


class ParticipantsListResponse(BaseModel):
    items: List[ParticipantResponse] = Field(..., description="Participants")


class NotificationResponse(BaseModel):
    id: str = Field(..., description="Notification UUID")
    user_id: str = Field(..., description="Target user UUID")
    huddle_id: Optional[str] = Field(None, description="Related huddle UUID")
    notification_type: str = Field(..., description="Type key")
    title: Optional[str] = Field(None, description="Title")
    body: Optional[str] = Field(None, description="Body")
    data: Dict[str, Any] = Field(default_factory=dict, description="Arbitrary JSON data")
    is_read: bool = Field(..., description="Read flag")
    created_at: datetime = Field(..., description="Created timestamp")
    read_at: Optional[datetime] = Field(None, description="Read timestamp")


class NotificationsListResponse(BaseModel):
    items: List[NotificationResponse] = Field(..., description="Notifications")


class MarkNotificationReadRequest(BaseModel):
    is_read: bool = Field(True, description="Set read state; default true")


# WebSocket payloads

WsChannel = Literal["chat", "events", "signaling"]


class WsEnvelope(BaseModel):
    type: str = Field(..., description="Message type discriminator")
    huddle_id: str = Field(..., description="Huddle UUID")
    sender_user_id: Optional[str] = Field(None, description="Sender user UUID (server populates when known)")
    payload: Dict[str, Any] = Field(default_factory=dict, description="Payload")


# WebRTC signaling types (payload lives in WsEnvelope.payload)
# offer/answer/ice and screen share start/stop are modeled by envelope.type:
# - webrtc_offer, webrtc_answer, webrtc_ice
# - screenshare_start, screenshare_stop
