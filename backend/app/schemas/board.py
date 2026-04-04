from datetime import datetime

from pydantic import BaseModel, Field
from typing import Optional, Any


class BoardCreate(BaseModel):
    title: str = Field(min_length=1, max_length=255)
    description: str 


class BoardUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    content: Optional[Any] = None


class BoardResponse(BaseModel):
    id: int
    title: str
    owner_id: int
    description: str
    created_at: datetime
    content: Optional[Any] = None

    class Config:
        from_attributes = True
