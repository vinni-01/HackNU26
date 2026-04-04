"""
app/dependencies/auth.py — Legacy shim kept for backward compatibility.

The canonical auth dependency is in app.deps.get_current_user.
This module simply re-exports from there so any old import paths
(app.dependencies.auth) continue to work without modification.
"""

from app.deps import get_current_user, oauth2_scheme

__all__ = ["get_current_user", "oauth2_scheme"]
