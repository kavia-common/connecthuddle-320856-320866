from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set, Tuple

from fastapi import WebSocket

RoomKey = Tuple[str, str]  # (huddle_id, channel)


@dataclass(frozen=True)
class ClientInfo:
    user_id: str


class WsRoomManager:
    """In-memory WebSocket room manager.

    Maintains per-(huddle, channel) connection sets. Persistence is handled separately
    (chat_messages table for chat; notifications table for some events).
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._rooms: Dict[RoomKey, Dict[WebSocket, ClientInfo]] = {}

    async def connect(self, websocket: WebSocket, huddle_id: str, channel: str, user_id: str) -> None:
        """Accept and register a websocket in a room."""
        await websocket.accept()
        async with self._lock:
            key = (huddle_id, channel)
            if key not in self._rooms:
                self._rooms[key] = {}
            self._rooms[key][websocket] = ClientInfo(user_id=user_id)

    async def disconnect(self, websocket: WebSocket) -> Optional[Tuple[str, str, str]]:
        """Remove a websocket from all rooms; returns (huddle_id, channel, user_id) if found."""
        async with self._lock:
            for (huddle_id, channel), conns in list(self._rooms.items()):
                if websocket in conns:
                    user_id = conns[websocket].user_id
                    del conns[websocket]
                    if not conns:
                        del self._rooms[(huddle_id, channel)]
                    return (huddle_id, channel, user_id)
        return None

    async def broadcast(self, huddle_id: str, channel: str, message: Dict[str, Any]) -> None:
        """Broadcast JSON to all clients in the room."""
        async with self._lock:
            conns = list(self._rooms.get((huddle_id, channel), {}).keys())
        for ws in conns:
            try:
                await ws.send_json(message)
            except Exception:
                # Best-effort; disconnect cleanup happens on receive loop.
                pass

    async def participants(self, huddle_id: str, channel: str) -> Set[str]:
        """Return user_ids currently connected for a room."""
        async with self._lock:
            conns = self._rooms.get((huddle_id, channel), {})
            return {info.user_id for info in conns.values()}


ws_manager = WsRoomManager()
