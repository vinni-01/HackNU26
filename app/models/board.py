from typing import Optional
from sqlmodel import SQLModel, Field


class Board(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    description: str | None = None
    owner_id: int = Field(index=True)
    room_id: str = Field(index=True, unique=True)