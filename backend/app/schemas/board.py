"""
app/schemas/board.py — Pydantic schemas for the Board resource.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel


class BoardCreate(BaseModel):
    title: str
    description: Optional[str] = None
    agent_mode: Literal["autonomous", "permission"] = "permission"


class BoardRead(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    owner_id: int
    room_id: str
    livekit_room_name: str
    created_at: datetime
    agent_mode: str

    model_config = {"from_attributes": True}


class AgentModeUpdate(BaseModel):
    agent_mode: Literal["autonomous", "permission"]