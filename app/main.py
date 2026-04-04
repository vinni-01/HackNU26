"""
app/main.py — FastAPI application root.

Lifespan:
  - On startup: validate Redis connectivity and run DB table creation.
  - On shutdown: close the Redis pool gracefully.

Routers registered:
  /auth         — registration, login, JWT refresh
  /boards       — board CRUD + agent mode control + live state
  /livekit      — participant token issuance
  /canvas       — WebSocket relay (tldraw DO <-> Redis pub/sub)
  /webhooks     — Higgsfield completion callback
  /memory       — Spatial RAG and snapshot history REST API
  /ai           — Gemini-powered board summarization
  /files        — Authenticated file upload to Cloudflare R2
  /users        — Current-user profile
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.database import create_db_and_tables
from app.routers.ai import router as ai_router
from app.routers.auth import router as auth_router
from app.routers.boards import router as boards_router
from app.routers.canvas import router as canvas_router
from app.routers.files import router as files_router
from app.routers.livekit_token import router as livekit_router
from app.routers.memory import router as memory_router
from app.routers.users import router as users_router
from app.routers.webhook import router as webhook_router

logger = logging.getLogger("app.main")
logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    logger.info("Database tables verified")

    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        await redis.ping()
        logger.info("Redis connection verified: %s", settings.redis_url)
    except Exception as exc:
        logger.warning("Redis not reachable at startup: %s", exc)
    finally:
        await redis.aclose()

    yield

    logger.info("Application shutdown")


app = FastAPI(
    title=settings.app_name,
    description=(
        "Production backend for the AI Brainstorm Canvas. "
        "Provides LiveKit token issuance, canvas WebSocket relay, "
        "Higgsfield async media pipeline, Spatial RAG, and snapshot management."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# In production, replace allow_origins with your actual frontend domain(s).
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.environment == "development" else [],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(boards_router)
app.include_router(livekit_router)
app.include_router(canvas_router)
app.include_router(webhook_router)
app.include_router(memory_router)
app.include_router(ai_router)
app.include_router(files_router)
app.include_router(users_router)


@app.get("/", tags=["health"])
def health() -> dict:
    return {"status": "ok", "service": settings.app_name}


@app.get("/health", tags=["health"])
async def health_detail() -> dict:
    redis_ok = False
    try:
        r = aioredis.from_url(settings.redis_url, decode_responses=True)
        await r.ping()
        await r.aclose()
        redis_ok = True
    except Exception:
        pass
    return {
        "status": "ok",
        "redis": "connected" if redis_ok else "unreachable",
        "environment": settings.environment,
    }
