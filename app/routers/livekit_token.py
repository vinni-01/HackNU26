"""
app/routers/livekit_token.py — LiveKit JWT issuance for canvas participants.

Route: POST /livekit/token
Returns a signed participant token scoped to a specific board's room.
The token grants publish + subscribe rights and embeds participant
metadata (display_name, user_id) so the agent can attribute audio correctly.
"""

from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from livekit.api import AccessToken, VideoGrants
from pydantic import BaseModel
from sqlmodel import Session, select

from app.core.config import settings
from app.core.database import get_session
from app.deps import get_current_user
from app.models.board import Board
from app.models.user import User

router = APIRouter(prefix="/livekit", tags=["livekit"])


class TokenRequest(BaseModel):
    board_id: int
    display_name: Optional[str] = None


class TokenResponse(BaseModel):
    token: str
    room_name: str
    ws_url: str
    expires_at: int


@router.post("/token", response_model=TokenResponse)
def issue_token(
    body: TokenRequest,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> TokenResponse:
    """
    Issue a LiveKit participant token for the requesting user.

    The token is scoped to the board's livekit_room_name so it matches
    the Cloudflare Durable Object ID for that canvas instance.
    """
    board = session.exec(
        select(Board).where(Board.id == body.board_id)
    ).first()
    if not board:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Board not found")

    # Any authenticated user may join a board (add ACL here if required).
    display_name = body.display_name or current_user.email.split("@")[0]

    ttl = settings.access_token_expire_minutes * 60
    expires_at = int(time.time()) + ttl

    token = (
        AccessToken(
            api_key=settings.livekit_api_key,
            api_secret=settings.livekit_api_secret,
        )
        .with_identity(str(current_user.id))
        .with_name(display_name)
        .with_grants(
            VideoGrants(
                room_join=True,
                room=board.livekit_room_name,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            )
        )
        .with_metadata(
            __import__("json").dumps({
                "user_id": current_user.id,
                "display_name": display_name,
                "board_id": board.id,
                "agent_mode": board.agent_mode,
            })
        )
        .with_ttl(__import__("datetime").timedelta(seconds=ttl))
        .to_jwt()
    )

    return TokenResponse(
        token=token,
        room_name=board.livekit_room_name,
        ws_url=settings.livekit_url,
        expires_at=expires_at,
    )
