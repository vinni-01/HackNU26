"""
app/schemas/auth.py — Auth-specific Pydantic models.

These are kept separate from app/schemas/user.py for clarity.
The canonical registration/login schema used by app/routers/auth.py
is in app/schemas/user.py (UserCreate, UserLogin, TokenResponse).
This module re-exports those for any code that imports from here.
"""

from app.schemas.user import TokenResponse, UserCreate as RegisterRequest, UserLogin as LoginRequest

__all__ = ["RegisterRequest", "LoginRequest", "TokenResponse"]