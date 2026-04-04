from pydantic import BaseModel


class BoardCreate(BaseModel):
    title: str
    description: str | None = None


class BoardRead(BaseModel):
    id: int
    title: str
    description: str | None = None
    owner_id: int
    room_id: str