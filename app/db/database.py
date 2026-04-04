"""
app/db/database.py — Legacy shim kept for backward compatibility.

The canonical database engine is now in app.core.database.
This module simply re-exports from there so any old import paths
(app.db.database) still work without modification.
"""

from app.core.database import create_db_and_tables, engine, get_session

# Legacy SQLAlchemy-style aliases used by older routers/dependencies
get_db = get_session

__all__ = ["engine", "get_session", "get_db", "create_db_and_tables"]