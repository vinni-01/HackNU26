import uuid
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.core.database import get_session
from app.deps import get_current_user
from app.models.board import Board
from app.models.user import User
from app.schemas.board import BoardCreate, BoardRead

router = APIRouter(prefix="/boards", tags=["boards"])


@router.post("", response_model=BoardRead, status_code=201)
def create_board(
    data: BoardCreate,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    board = Board(
        title=data.title,
        description=data.description,
        owner_id=current_user.id,
        room_id=str(uuid.uuid4()),
    )
    session.add(board)
    session.commit()
    session.refresh(board)
    return board


@router.get("", response_model=list[BoardRead])
def list_my_boards(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    boards = session.exec(
        select(Board).where(Board.owner_id == current_user.id)
    ).all()
    return boards


@router.get("/{board_id}", response_model=BoardRead)
def get_board(
    board_id: int,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    board = session.get(Board, board_id)
    if not board:
        raise HTTPException(status_code=404, detail="Board not found")
    if board.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Forbidden")
    return board