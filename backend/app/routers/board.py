from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db.database import get_db
from app.dependencies.auth import get_current_user
from app.models.board import Board
from app.models.user import User
from app.schemas.board import BoardCreate, BoardResponse, BoardUpdate

router = APIRouter(prefix="/boards", tags=["boards"])


@router.post("", response_model=BoardResponse, status_code=status.HTTP_201_CREATED)
def create_board(
    board_data: BoardCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    new_board = Board(
        title=board_data.title,
        owner_id=current_user.id,
        content = None,
    )

    db.add(new_board)
    db.commit()
    db.refresh(new_board)

    return new_board


@router.get("", response_model=list[BoardResponse])
def list_my_boards(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    boards = (
        db.query(Board)
        .filter(Board.owner_id == current_user.id)
        .order_by(Board.created_at.desc())
        .all()
    )
    return boards


@router.get("/{board_id}", response_model=BoardResponse)
def get_board(
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

    return board


@router.put("/{board_id}", response_model=BoardResponse)
def update_board(
    board_id: int,
    board_data: BoardUpdate,
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

    board.title = board_data.title
    board.description = board_data.description
    board.content = board_data.content
    db.add(board)
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
