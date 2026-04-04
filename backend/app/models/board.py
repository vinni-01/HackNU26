"""
app/models/board.py — SQLModel Board table.

Added fields:
  - livekit_room_name: canonical room name used by the LiveKit agent and token API.
    This is also the Cloudflare Durable Object ID that owns this canvas instance.
  - created_at: ISO-8601 creation timestamp for snapshot attribution.
  - agent_mode: controls the agent's editorial behaviour per-board
    ("autonomous" = acts without asking; "permission" = requests approval before edits).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


class Board(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    description: Optional[str] = None
    owner_id: int = Field(index=True)

    # room_id is the legacy UUID; kept for backward compatibility.
    room_id: str = Field(index=True, unique=True)

    # livekit_room_name is the authoritative identifier shared across
    # LiveKit, the Cloudflare DO, and the agent session.
    livekit_room_name: str = Field(index=True, unique=True)

    created_at: datetime = Field(default_factory=_utcnow)

    # "autonomous" | "permission"
    agent_mode: str = Field(default="permission")