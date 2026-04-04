"""
app/routers/boards.py — Board CRUD with LiveKit room management.

New in this revision:
  - POST /boards: generates a unique livekit_room_name (= DO room ID) on creation.
  - PATCH /boards/{board_id}/mode: switch agent_mode at runtime.
  - GET /boards/{board_id}/state: return the current tldraw document state from
    the Redis cache (written by the canvas WebSocket relay).
"""

from __future__ import annotations

import uuid

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from app.core.config import settings
from app.core.database import get_session
from app.deps import get_current_user
from app.models.board import Board
from app.models.user import User
from app.schemas.board import AgentModeUpdate, BoardCreate, BoardRead

router = APIRouter(prefix="/boards", tags=["boards"])


def _redis() -> aioredis.Redis:
    return aioredis.from_url(settings.redis_url, decode_responses=True)



@router.post("", response_model=BoardRead, status_code=status.HTTP_201_CREATED)
def create_board(
    data: BoardCreate,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> Board:
    unique_id = uuid.uuid4().hex
    board = Board(
        title=data.title,
        description=data.description,
        owner_id=current_user.id,
        room_id=unique_id,
        livekit_room_name=f"board-{unique_id}",
        agent_mode=data.agent_mode,
    )
    session.add(board)
    session.commit()
    session.refresh(board)
    return board



@router.get("", response_model=list[BoardRead])
def list_my_boards(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[Board]:
    return list(session.exec(select(Board).where(Board.owner_id == current_user.id)).all())



@router.get("/{board_id}", response_model=BoardRead)
def get_board(
    board_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> Board:
    board = _get_board_or_404(board_id, current_user.id, session)
    return board



@router.patch("/{board_id}/mode", response_model=BoardRead)
def update_agent_mode(
    board_id: int,
    body: AgentModeUpdate,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> Board:
    """
    Switch a board's agent_mode between 'autonomous' and 'permission' at runtime.
    The agent reads this value from Redis on each decision cycle.
    """
    board = _get_board_or_404(board_id, current_user.id, session)
    board.agent_mode = body.agent_mode
    session.add(board)
    session.commit()
    session.refresh(board)
    return board



@router.get("/{board_id}/state")
async def get_board_state(
    board_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict:
    """
    Return the current tldraw document state for a board.
    The canvas WebSocket relay writes the latest DO snapshot into
    Redis at key  board:state:{livekit_room_name}.
    """
    board = _get_board_or_404(board_id, current_user.id, session)
    redis = _redis()
    try:
        raw = await redis.get(f"board:state:{board.livekit_room_name}")
    finally:
        await redis.aclose()

    if not raw:
        return {"board_id": board_id, "state": None}

    import json
    return {"board_id": board_id, "state": json.loads(raw)}



def _get_board_or_404(board_id: int, user_id: int, session: Session) -> Board:
    board = session.get(Board, board_id)
    if not board:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Board not found")
    if board.owner_id != user_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")
    return board