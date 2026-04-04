import uuid
from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from app.core.database import get_session
from app.db.database import get_db
from app.dependencies.auth import get_current_user
from app.models.board import Board
from app.models.user import User
from app.schemas.board import BoardCreate, BoardResponse, BoardUpdate

router = APIRouter(prefix="/boards", tags=["boards"])


@router.post("", response_model=BoardResponse, status_code=201)
def create_board(
    data: BoardCreate,
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    board = Board(
        title=data.title,
        description=data.description,
        owner_id=current_user.id,
        content=None,
    )
    session.add(board)
    session.commit()
    session.refresh(board)
    return board


@router.get("", response_model=list[BoardResponse])
def list_my_boards(
    session: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
):
    boards = session.exec(select(Board).where(Board.owner_id == current_user.id)).all()
    return boards


@router.get("/{board_id}", response_model=BoardResponse)
def get_board(
    board_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    board = db.query(Board).filter(Board.id == board_id).first()

    if not board:
        raise HTTPException(status_code=404, detail="Board not found")

    return board


@router.put("/{board_id}", response_model=BoardResponse)
def update_board(
    board_id: str,
    payload: BoardUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    board = db.query(Board).filter(Board.id == board_id).first()

    if not board:
        raise HTTPException(status_code=404, detail="Board not found")

    if payload.title is not None:
        board.title = payload.title

    if payload.content is not None:
        board.content = payload.content

    if payload.description is not None:
        board.description = payload.description

    db.commit()
    db.refresh(board)
    return board


@router.delete("/{board_id}", status_code=status.HTTP_200_OK)
def delete_board(
    board_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    board = (
        db.query(Board)
        .filter(Board.id == board_id, Board.owner_id == current_user.id)
        .first()
    )

    if not board:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Board not found",
        )

    db.delete(board)
    db.commit()

    return {"message": "Board deleted successfully"}
