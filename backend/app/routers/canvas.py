"""
app/routers/canvas.py — WebSocket relay: tldraw Cloudflare DO <-> Redis pub/sub.

Architecture:
  - The frontend connects to wss://your-cf-worker.workers.dev/sync (Cloudflare DO).
  - The TypeScript Cloudflare Worker forwards a sanitized event stream to this
    FastAPI WebSocket endpoint via a persistent hidden participant connection.
  - This relay publishes events to Redis channel canvas:events:{room_name}.
  - The agent's CanvasEventSubscriber reads from that channel.
  - Feedback mitigation: events with source='agent' are filtered BEFORE publishing
    to Redis so the agent never reacts to its own canvas mutations.

Additionally this endpoint:
  - Writes the latest board state snapshot to Redis key board:state:{room_name}.
  - Broadcasts agent_shape and revert events received from Redis back out to
    all connected WebSocket clients on that board.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import redis.asyncio as aioredis
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from app.core.config import settings

logger = logging.getLogger("canvas.relay")
router = APIRouter(prefix="/canvas", tags=["canvas"])



class ConnectionManager:
    """Tracks all active WebSocket connections per room."""

    def __init__(self) -> None:
        # room_name -> set of WebSocket connections
        self._rooms: dict[str, set[WebSocket]] = {}

    async def connect(self, room: str, ws: WebSocket) -> None:
        await ws.accept()
        self._rooms.setdefault(room, set()).add(ws)
        logger.info("WS connected: room=%s total=%d", room, len(self._rooms[room]))

    def disconnect(self, room: str, ws: WebSocket) -> None:
        self._rooms.get(room, set()).discard(ws)

    async def broadcast(self, room: str, message: str, exclude: Optional[WebSocket] = None) -> None:
        dead: set[WebSocket] = set()
        for ws in list(self._rooms.get(room, set())):
            if ws is exclude:
                continue
            try:
                await ws.send_text(message)
            except Exception:
                dead.add(ws)
        for ws in dead:
            self._rooms.get(room, set()).discard(ws)


manager = ConnectionManager()



def _make_redis() -> aioredis.Redis:
    return aioredis.from_url(settings.redis_url, decode_responses=True)


async def _publish_canvas_event(redis: aioredis.Redis, room: str, event: dict) -> None:
    await redis.publish(f"canvas:events:{room}", json.dumps(event))


async def _cache_board_state(redis: aioredis.Redis, room: str, state: dict) -> None:
    # Keep the latest board snapshot in Redis with a 24-hour TTL.
    await redis.set(f"board:state:{room}", json.dumps(state), ex=86400)



@router.websocket("/ws/{room_name}")
async def canvas_ws(ws: WebSocket, room_name: str) -> None:
    """
    Each client (including the Cloudflare Worker bridge) connects here.

    Message protocol (JSON):
      Inbound (client -> relay):
        { "type": "shape_added"|"shape_removed"|"cursor_move"|"board_state"|"new_cluster",
          "source": "human"|"agent",
          ... }

      Outbound (relay -> client):
        { "type": "agent_shape"|"revert"|"presence"|... }
    """
    redis = _make_redis()
    await manager.connect(room_name, ws)

    # Background task: subscribe to Redis and push agent events back to clients.
    relay_task = asyncio.create_task(_redis_to_ws(redis, room_name, ws))

    try:
        while True:
            raw = await ws.receive_text()
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Invalid JSON from WS client in room %s", room_name)
                continue

            source = (
                event.get("source")
                or event.get("shape", {}).get("meta", {}).get("source")
            )

            # Feedback mitigation: never re-publish agent-originated events.
            if source == "agent":
                logger.debug("Dropping agent-sourced event type=%s", event.get("type"))
                continue

            # Cache full board state snapshots for the agent and REST API.
            if event.get("type") == "board_state":
                state = event.get("state")
                if state:
                    await _cache_board_state(redis, room_name, state)

            # Publish human events to the agent's Redis subscription.
            await _publish_canvas_event(redis, room_name, event)

            # Broadcast to other WebSocket clients on this room (presence, etc.)
            payload = json.dumps(event)
            await manager.broadcast(room_name, payload, exclude=ws)

    except WebSocketDisconnect:
        logger.info("WS disconnected: room=%s", room_name)
    except Exception as exc:
        logger.error("WS error in room %s: %s", room_name, exc, exc_info=True)
    finally:
        relay_task.cancel()
        manager.disconnect(room_name, ws)
        await redis.aclose()


async def _redis_to_ws(redis: aioredis.Redis, room_name: str, ws: WebSocket) -> None:
    """
    Subscribe to Redis for agent-originated canvas events and relay them to
    all WebSocket clients connected to the same room.

    Events published back to clients:
      - agent_shape: a Ghost Shape placement from the agent
      - revert: a time-travel board revert triggered by the agent
      - permission_request: agent asking for user approval before editing
    """
    channel = f"canvas:events:{room_name}"
    pubsub = redis.pubsub()
    await pubsub.subscribe(channel)
    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                event = json.loads(message["data"])
            except Exception:
                continue

            event_type = event.get("type")
            # Only relay agent-generated events back out to clients.
            if event_type in {"agent_shape", "revert", "permission_request"}:
                await manager.broadcast(room_name, json.dumps(event))
    except asyncio.CancelledError:
        pass
    finally:
        await pubsub.unsubscribe(channel)
