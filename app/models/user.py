"""
app/models/user.py — SQLModel User table.

Migrated from legacy SQLAlchemy declarative Base to SQLModel so it
integrates cleanly with app.core.database (SQLModel engine) and
can be used directly in FastAPI route type annotations.
"""

from __future__ import annotations

from typing import Optional

from sqlmodel import Field, SQLModel


class User(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    hashed_password: str