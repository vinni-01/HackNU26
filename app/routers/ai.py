from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.deps import get_current_user
from app.models.user import User

router = APIRouter(prefix="/ai", tags=["ai"])


class BoardSummaryRequest(BaseModel):
    board_title: str
    notes: list[str]


class BoardSummaryResponse(BaseModel):
    summary: str
    next_steps: list[str]


@router.post("/summarize", response_model=BoardSummaryResponse)
def summarize_board(
    data: BoardSummaryRequest,
    current_user: User = Depends(get_current_user),
):
    summary = f"Board '{data.board_title}' has {len(data.notes)} notes."
    next_steps = [
        "Group related notes",
        "Turn grouped notes into tasks",
        "Assign owners",
    ]
    return BoardSummaryResponse(summary=summary, next_steps=next_steps)