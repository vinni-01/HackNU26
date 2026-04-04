"""
agent/perception_loop.py — Periodic canvas perception and Spatial RAG ingestion.

Runs as a background asyncio task inside the agent session.
Every PERCEPTION_INTERVAL seconds it:
  1. Fetches the current board state JSON from Redis (written by canvas relay).
  2. Builds a ShapePerceptionTier via PromptPartUtil (Focused / Blurry / Peripheral).
  3. Embeds each new peripheral cluster into Cloudflare Vectorize (SpatialRAG).
  4. Publishes the compressed tier summary to Redis for consumption by the
     agent's Gemini context injection on the next conversational turn.
  5. Detects snapshot_request events and triggers an out-of-schedule snapshot.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Optional

import redis.asyncio as aioredis

from agent.config import caching_config, spatial_config
from agent.spatial_utils import (
    CanvasBounds,
    PromptPartUtil,
    SpatialRAG,
    SnapshotManager,
    perception_tier_to_prompt,
)

logger = logging.getLogger("brainstorm.perception")

PERCEPTION_INTERVAL = 2.0        # seconds between perception cycles
CLUSTER_EMBED_COOLDOWN = 30.0    # min seconds between re-embedding the same cluster region


class PerceptionLoop:
    """
    Background task that keeps the agent's spatial awareness current.

    Attributes:
        board_id      : the LiveKit room / Cloudflare DO identifier
        canvas_w      : canvas width in pixels
        canvas_h      : canvas height in pixels
        viewport      : current agent viewport (updated from Redis presence data)
    """

    def __init__(
        self,
        board_id: str,
        redis_client: aioredis.Redis,
        rag: SpatialRAG,
        snapshot_mgr: SnapshotManager,
        canvas_w: float = 4096.0,
        canvas_h: float = 4096.0,
    ) -> None:
        self.board_id = board_id
        self._redis = redis_client
        self._rag = rag
        self._snapshot = snapshot_mgr
        self.canvas_w = canvas_w
        self.canvas_h = canvas_h

        # Default viewport covers the centre third of the canvas.
        self.viewport = CanvasBounds(
            x=canvas_w * 0.33,
            y=canvas_h * 0.33,
            w=canvas_w * 0.34,
            h=canvas_h * 0.34,
        )

        self._task: Optional[asyncio.Task] = None
        self._last_embed_time: dict[str, float] = {}   # cluster_id -> timestamp
        self._last_tier_summary: str = ""


    def update_viewport(self, x: float, y: float, w: float, h: float) -> None:
        """Called when the agent receives a viewport update from the canvas."""
        self.viewport = CanvasBounds(x=x, y=y, w=w, h=h)

    async def get_latest_summary(self) -> str:
        """
        Return the most recent perception tier summary from Redis.
        Falls back to the in-memory cache if Redis is unavailable.
        """
        try:
            raw = await self._redis.get(f"perception:summary:{self.board_id}")
            if raw:
                return raw
        except Exception:
            pass
        return self._last_tier_summary

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())
        logger.info("PerceptionLoop started for board %s", self.board_id)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("PerceptionLoop stopped for board %s", self.board_id)


    async def _loop(self) -> None:
        while True:
            try:
                await self._cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Perception cycle error: %s", exc, exc_info=True)
            await asyncio.sleep(PERCEPTION_INTERVAL)

    async def _cycle(self) -> None:
        raw = await self._redis.get(f"board:state:{self.board_id}")
        if not raw:
            return

        try:
            state = json.loads(raw)
        except json.JSONDecodeError:
            return

        shapes: list[dict] = state.get("shapes", []) if isinstance(state, dict) else []
        if not shapes:
            return

        util = PromptPartUtil(
            canvas_w=self.canvas_w,
            canvas_h=self.canvas_h,
            viewport=self.viewport,
        )
        tier = util.build(shapes)

        summary = perception_tier_to_prompt(tier)
        self._last_tier_summary = summary
        await self._redis.set(
            f"perception:summary:{self.board_id}",
            summary,
            ex=30,  # expires after 30 s — always re-generated
        )

        now = time.time()
        for cluster in tier.peripheral_clusters:
            cluster_key = f"{cluster['coords']['x_min']}:{cluster['coords']['y_min']}"
            cluster_id = f"{self.board_id}:{cluster_key}"
            last_embedded = self._last_embed_time.get(cluster_id, 0.0)
            if now - last_embedded < CLUSTER_EMBED_COOLDOWN:
                continue
            success = await self._rag.upsert_cluster(
                cluster_id=cluster_id,
                cluster=cluster,
                board_id=self.board_id,
            )
            if success:
                self._last_embed_time[cluster_id] = now

        snap_flag = await self._redis.get(f"snapshot_request:{self.board_id}")
        if snap_flag:
            await self._redis.delete(f"snapshot_request:{self.board_id}")
            snapshot_id = await self._snapshot.take_snapshot()
            if snapshot_id:
                logger.info("Out-of-schedule snapshot taken: %s", snapshot_id)
