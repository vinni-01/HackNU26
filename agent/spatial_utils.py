"""
spatial_utils.py R-Tree collision avoidance, tiered canvas perception, 
coordinate normalization, and Hierarchical Spatial RAG.

Dependencies: rtree, numpy, httpx, sentence-transformers (local embedding fallback)
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx
import numpy as np

# R-Tree spatial index (pip install rtree)
try:
    from rtree import index as rtree_index
    RTREE_AVAILABLE = True
except ImportError:
    RTREE_AVAILABLE = False
    logging.warning("rtree not installed — collision avoidance disabled")

from agent.config import env, spatial_config

logger = logging.getLogger(__name__)




@dataclass
class CanvasBounds:
    """Axis-aligned bounding box in canvas pixel space."""
    x: float
    y: float
    w: float
    h: float

    @property
    def x2(self) -> float:
        return self.x + self.w

    @property
    def y2(self) -> float:
        return self.y + self.h

    def as_rtree_tuple(self) -> tuple[float, float, float, float]:
        """(left, bottom, right, top) for R-Tree insertion."""
        return (self.x, self.y, self.x2, self.y2)

    def expanded(self, buffer: float) -> "CanvasBounds":
        return CanvasBounds(
            x=self.x - buffer,
            y=self.y - buffer,
            w=self.w + 2 * buffer,
            h=self.h + 2 * buffer,
        )


def normalize_to_gemini(
    bounds: CanvasBounds,
    canvas_w: float,
    canvas_h: float,
    coord_max: int = 1000,
) -> dict[str, int]:
    """
    Convert canvas pixel bounds → Gemini Cartesian format [y_min, x_min, y_max, x_max]
    in [0, coord_max] range.

    Note: Gemini uses (row, col) ordering i.e. y first.
    """
    def clamp(v: float) -> int:
        return max(0, min(coord_max, int(v)))

    x_min = clamp((bounds.x / canvas_w) * coord_max)
    y_min = clamp((bounds.y / canvas_h) * coord_max)
    x_max = clamp((bounds.x2 / canvas_w) * coord_max)
    y_max = clamp((bounds.y2 / canvas_h) * coord_max)

    return {"y_min": y_min, "x_min": x_min, "y_max": y_max, "x_max": x_max}


def gemini_to_canvas(
    gemini_coords: dict[str, int],
    canvas_w: float,
    canvas_h: float,
    coord_max: int = 1000,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
) -> CanvasBounds:
    """
    Convert Gemini [y_min, x_min, y_max, x_max] → canvas pixel CanvasBounds.
    Applies removeOffsetFromVec equivalent via offset_x / offset_y.
    """
    x = (gemini_coords["x_min"] / coord_max) * canvas_w + offset_x
    y = (gemini_coords["y_min"] / coord_max) * canvas_h + offset_y
    x2 = (gemini_coords["x_max"] / coord_max) * canvas_w + offset_x
    y2 = (gemini_coords["y_max"] / coord_max) * canvas_h + offset_y
    return CanvasBounds(x=x, y=y, w=x2 - x, h=y2 - y)




class RTreeIndex:
    """
    Thread-safe R-Tree wrapper for spatial occupancy queries.

    Usage:
        idx = RTreeIndex()
        idx.insert(shape_id="abc", bounds=CanvasBounds(100, 200, 300, 150))
        hits = idx.query(bounds=CanvasBounds(90, 190, 320, 160), buffer=24)
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._id_map: dict[str, int] = {}  # shape_id → numeric int id
        self._reverse_map: dict[int, str] = {}
        self._counter: int = 0

        if RTREE_AVAILABLE:
            p = rtree_index.Property()
            p.dimension = 2
            p.dat_extension = "data"
            p.idx_extension = "index"
            self._idx = rtree_index.Index(properties=p)
        else:
            # Fallback: brute-force list
            self._shapes: list[tuple[int, tuple[float, float, float, float]]] = []

    def _next_id(self) -> int:
        self._counter += 1
        return self._counter

    async def insert(self, shape_id: str, bounds: CanvasBounds) -> None:
        async with self._lock:
            int_id = self._id_map.get(shape_id)
            if int_id is None:
                int_id = self._next_id()
                self._id_map[shape_id] = int_id
                self._reverse_map[int_id] = shape_id

            coords = bounds.as_rtree_tuple()
            if RTREE_AVAILABLE:
                self._idx.insert(int_id, coords)
            else:
                # Remove old entry if exists
                self._shapes = [(i, c) for i, c in self._shapes if i != int_id]
                self._shapes.append((int_id, coords))

    async def delete(self, shape_id: str, bounds: CanvasBounds) -> None:
        async with self._lock:
            int_id = self._id_map.pop(shape_id, None)
            if int_id is None:
                return
            self._reverse_map.pop(int_id, None)
            if RTREE_AVAILABLE:
                try:
                    self._idx.delete(int_id, bounds.as_rtree_tuple())
                except Exception:
                    pass
            else:
                self._shapes = [(i, c) for i, c in self._shapes if i != int_id]

    async def query(
        self, bounds: CanvasBounds, buffer: float = 0.0
    ) -> list[str]:
        """
        Return shape_ids whose bounding boxes intersect bounds (+ safety buffer).
        """
        expanded = bounds.expanded(buffer)
        coords = expanded.as_rtree_tuple()

        async with self._lock:
            if RTREE_AVAILABLE:
                hits = list(self._idx.intersection(coords))
            else:
                hits = [
                    i for i, c in self._shapes
                    if not (c[2] < coords[0] or c[0] > coords[2]
                            or c[3] < coords[1] or c[1] > coords[3])
                ]
            return [self._reverse_map[i] for i in hits if i in self._reverse_map]

    async def is_occupied(
        self, bounds: CanvasBounds, buffer: float | None = None
    ) -> bool:
        if buffer is None:
            buffer = spatial_config.safety_buffer_px
        hits = await self.query(bounds, buffer=buffer)
        return len(hits) > 0

    async def find_open_area(
        self,
        preferred: CanvasBounds,
        step: float = 50.0,
        max_attempts: int = 40,
    ) -> CanvasBounds:
        """
        Spiral-search for an unoccupied area near `preferred`.
        Returns the first clear CanvasBounds found, or preferred if exhausted.
        """
        candidates = _spiral_offsets(max_attempts, step)
        for dx, dy in candidates:
            candidate = CanvasBounds(
                x=preferred.x + dx,
                y=preferred.y + dy,
                w=preferred.w,
                h=preferred.h,
            )
            if not await self.is_occupied(candidate):
                return candidate
        return preferred  # fallback, accept overlap risk


def _spiral_offsets(n: int, step: float) -> list[tuple[float, float]]:
    """Generate spiral search offsets: (dx, dy) pairs."""
    offsets: list[tuple[float, float]] = []
    x, y = 0.0, 0.0
    dx, dy = step, 0.0
    for _ in range(n):
        offsets.append((x, y))
        x += dx
        y += dy
        if x == y or (x < 0 and x == -y) or (x > 0 and x == 1 - y):
            dx, dy = -dy, dx
    return offsets




@dataclass
class ShapePerceptionTier:
    """
    Output of PromptPartUtil.build() - passed directly into Gemini context.
    """
    focused: list[dict]         # Full metadata
    blurry: list[dict]          # Bounds + primary text
    peripheral_clusters: list[dict]  # Bounding boxes + counts + theme


class PromptPartUtil:
    """
    Converts raw tldraw shape data into tiered perception dicts
    suitable for inclusion in a Gemini content part.

    Input shape format (from tldraw JSON):
    {
      "id": "shape:xxx",
      "type": "note" | "text" | "geo" | "image" | ...,
      "x": 120, "y": 340, "props": { "text": "...", "color": "...", ... },
      "meta": { "owner": "user_alice", "source": "human", ... },
      "selected": true,
      "inViewport": true,
    }
    """

    def __init__(
        self,
        canvas_w: float,
        canvas_h: float,
        viewport: CanvasBounds,
        cluster_radius: float | None = None,
    ) -> None:
        self.canvas_w = canvas_w
        self.canvas_h = canvas_h
        self.viewport = viewport
        self.cluster_radius = cluster_radius or spatial_config.rag_cluster_radius_px

    def build(self, shapes: list[dict]) -> ShapePerceptionTier:
        focused: list[dict] = []
        blurry: list[dict] = []
        peripheral_raw: list[dict] = []

        for shape in shapes:
            bounds = self._shape_bounds(shape)
            if not bounds:
                continue

            gemini_coords = normalize_to_gemini(
                bounds, self.canvas_w, self.canvas_h
            )

            if shape.get("selected", False):
                # TIER 1 — FocusedShape: everything
                focused.append({
                    "tier": "focused",
                    "id": shape["id"],
                    "type": shape.get("type"),
                    "coords": gemini_coords,
                    "text": shape.get("props", {}).get("text", ""),
                    "color": shape.get("props", {}).get("color"),
                    "owner": shape.get("meta", {}).get("owner"),
                    "source": shape.get("meta", {}).get("source"),
                    "connections": shape.get("meta", {}).get("connections", []),
                    "raw_props": shape.get("props", {}),
                })

            elif self._in_viewport(bounds):
                # TIER 2 - BlurryShape: bounds + primary text
                text = shape.get("props", {}).get("text", "")
                blurry.append({
                    "tier": "blurry",
                    "id": shape["id"],
                    "type": shape.get("type"),
                    "coords": gemini_coords,
                    "text": text[:80] if text else "",  # truncate for token efficiency
                    "owner": shape.get("meta", {}).get("owner"),
                })

            else:
                # TIER 3 - Peripheral: aggregate later
                peripheral_raw.append({
                    "bounds": bounds,
                    "text": shape.get("props", {}).get("text", ""),
                    "type": shape.get("type"),
                })

        peripheral_clusters = self._cluster_peripheral(peripheral_raw)

        return ShapePerceptionTier(
            focused=focused,
            blurry=blurry,
            peripheral_clusters=peripheral_clusters,
        )

    def _shape_bounds(self, shape: dict) -> Optional[CanvasBounds]:
        try:
            x = float(shape.get("x", 0))
            y = float(shape.get("y", 0))
            props = shape.get("props", {})
            w = float(props.get("w", props.get("width", 200)))
            h = float(props.get("h", props.get("height", 100)))
            return CanvasBounds(x=x, y=y, w=w, h=h)
        except (TypeError, ValueError):
            return None

    def _in_viewport(self, bounds: CanvasBounds) -> bool:
        vp = self.viewport
        return not (
            bounds.x2 < vp.x or bounds.x > vp.x2
            or bounds.y2 < vp.y or bounds.y > vp.y2
        )

    def _cluster_peripheral(self, shapes: list[dict]) -> list[dict]:
        """
        Simple greedy clustering: group shapes within cluster_radius of each other.
        Returns PeripheralShapeCluster dicts.
        """
        if not shapes:
            return []

        assigned = [False] * len(shapes)
        clusters: list[dict] = []
        r = self.cluster_radius

        for i, shape in enumerate(shapes):
            if assigned[i]:
                continue
            group = [shape]
            assigned[i] = True
            cx = shape["bounds"].x + shape["bounds"].w / 2
            cy = shape["bounds"].y + shape["bounds"].h / 2

            for j, other in enumerate(shapes):
                if assigned[j]:
                    continue
                ox = other["bounds"].x + other["bounds"].w / 2
                oy = other["bounds"].y + other["bounds"].h / 2
                if math.hypot(cx - ox, cy - oy) <= r:
                    group.append(other)
                    assigned[j] = True

            # Compute cluster bounding box
            all_x = [s["bounds"].x for s in group] + [s["bounds"].x2 for s in group]
            all_y = [s["bounds"].y for s in group] + [s["bounds"].y2 for s in group]
            cluster_bounds = CanvasBounds(
                x=min(all_x), y=min(all_y),
                w=max(all_x) - min(all_x),
                h=max(all_y) - min(all_y),
            )
            gemini_coords = normalize_to_gemini(
                cluster_bounds, self.canvas_w, self.canvas_h
            )

            # Extract dominant theme from most common words
            all_text = " ".join(s["text"] for s in group if s["text"])
            theme = _dominant_theme(all_text)

            clusters.append({
                "tier": "peripheral_cluster",
                "count": len(group),
                "coords": gemini_coords,
                "dominant_theme": theme,
                "types": list({s["type"] for s in group}),
            })

        return clusters


def _dominant_theme(text: str, top_n: int = 3) -> str:
    """Extract top N most frequent tokens (trivial NLP - avoids heavy deps)."""
    STOP_WORDS = {"the", "a", "an", "and", "or", "but", "is", "in", "on", "at",
                  "to", "of", "for", "with", "that", "this", "as", "by", "from"}
    words = [w.lower().strip(".,!?;:\"'") for w in text.split()]
    words = [w for w in words if w and w not in STOP_WORDS and len(w) > 2]
    if not words:
        return "mixed content"
    freq: dict[str, int] = {}
    for w in words:
        freq[w] = freq.get(w, 0) + 1
    top = sorted(freq, key=lambda k: -freq[k])[:top_n]
    return ", ".join(top)


def perception_tier_to_prompt(tier: ShapePerceptionTier) -> str:
    """
    Serialize a ShapePerceptionTier into a compact, token-efficient string
    suitable for injection into a Gemini system/user turn.
    """
    parts: list[str] = []

    if tier.focused:
        parts.append("=== FOCUSED (selected shapes — full detail) ===")
        for s in tier.focused:
            parts.append(
                f"[{s['id']}] {s['type']} | owner={s['owner']} | "
                f"coords={s['coords']} | text=\"{s['text'][:120]}\" | "
                f"color={s['color']} | connections={s['connections']}"
            )

    if tier.blurry:
        parts.append("=== BLURRY (viewport shapes — bounds + text) ===")
        for s in tier.blurry:
            parts.append(
                f"[{s['id']}] {s['type']} | owner={s['owner']} | "
                f"coords={s['coords']} | text=\"{s['text']}\""
            )

    if tier.peripheral_clusters:
        parts.append("=== PERIPHERAL (distant clusters — global awareness) ===")
        for c in tier.peripheral_clusters:
            parts.append(
                f"Cluster({c['count']} shapes) at {c['coords']} | "
                f"theme: {c['dominant_theme']} | types: {c['types']}"
            )

    return "\n".join(parts) if parts else "(canvas is empty)"




def canvas_to_stereo_pan(
    speaker_x: float,
    listener_x: float,
    canvas_w: float,
) -> float:
    """
    Map relative X position → stereo pan value in [-1.0, +1.0].
    Positive = right, negative = left.
    """
    relative = (speaker_x - listener_x) / (canvas_w / 2.0)
    return max(-1.0, min(1.0, relative))


def canvas_to_volume(
    speaker_pos: tuple[float, float],
    listener_pos: tuple[float, float],
    max_distance: float | None = None,
) -> float:
    """
    Distance-based volume attenuation: volume = max(0, 1 - dist / max_distance).
    Mimics physical workspace acoustics.
    """
    if max_distance is None:
        max_distance = spatial_config.max_distance_px
    dist = math.hypot(
        speaker_pos[0] - listener_pos[0],
        speaker_pos[1] - listener_pos[1],
    )
    return max(0.0, 1.0 - dist / max_distance)




class SnapshotManager:
    """
    Periodically serializes tldraw board state to JSON and uploads to R2.
    Each snapshot gets a unique ID bound to (board_id, timestamp).
    """

    def __init__(self, board_id: str, r2_client: Any) -> None:
        self.board_id = board_id
        self.r2 = r2_client
        self._task: Optional[asyncio.Task] = None
        self._current_state: Optional[dict] = None
        self._lock = asyncio.Lock()

    def update_state(self, state: dict) -> None:
        """Called by canvas relay whenever a new tldraw patch arrives."""
        self._current_state = state

    async def start(self, interval: int = 60) -> None:
        self._task = asyncio.create_task(self._loop(interval))
        logger.info("SnapshotManager started for board %s", self.board_id)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self, interval: int) -> None:
        while True:
            await asyncio.sleep(interval)
            await self.take_snapshot()

    async def take_snapshot(self) -> Optional[str]:
        async with self._lock:
            if not self._current_state:
                return None
            snapshot_id = f"{self.board_id}/{int(time.time())}_{uuid.uuid4().hex[:8]}"
            key = f"snapshots/{snapshot_id}.json"
            body = json.dumps(self._current_state).encode()
            try:
                await asyncio.to_thread(
                    self.r2.put_object,
                    Bucket=env.r2_bucket,
                    Key=key,
                    Body=body,
                    ContentType="application/json",
                )
                logger.info("Snapshot saved: %s", key)
                return snapshot_id
            except Exception as e:
                logger.error("Snapshot upload failed: %s", e)
                return None

    async def fetch_snapshot(self, snapshot_id: str) -> Optional[dict]:
        key = f"snapshots/{snapshot_id}.json"
        try:
            resp = await asyncio.to_thread(
                self.r2.get_object,
                Bucket=env.r2_bucket,
                Key=key,
            )
            data = resp["Body"].read()
            return json.loads(data)
        except Exception as e:
            logger.error("Snapshot fetch failed for %s: %s", snapshot_id, e)
            return None




class SpatialRAG:
    """
    Embeds 'spatial clusters' (groups of shapes within a radius) and stores
    them in Cloudflare Vectorize for semantic retrieval.

    Query: "that cluster of revenue ideas in the top-right" →
           returns relevant cluster IDs + shape lists.

    Embedding: Uses a local SentenceTransformer (fallback) or Gemini embedding.
    """

    _BASE = "https://api.cloudflare.com/client/v4/accounts/{account_id}/vectorize/v2/indexes/{index}"

    def __init__(self) -> None:
        self._headers = {
            "Authorization": f"Bearer {env.vectorize_api_token}",
            "Content-Type": "application/json",
        }
        self._base_url = self._BASE.format(
            account_id=env.vectorize_account_id,
            index=env.vectorize_index,
        )
        self._embedder: Any = None

    def _get_embedder(self) -> Any:
        if self._embedder is None:
            try:
                from sentence_transformers import SentenceTransformer
                self._embedder = SentenceTransformer("all-MiniLM-L6-v2")
                logger.info("SpatialRAG: using local SentenceTransformer")
            except ImportError:
                logger.warning("SpatialRAG: sentence-transformers not available")
        return self._embedder

    def _embed(self, text: str) -> list[float]:
        embedder = self._get_embedder()
        if embedder:
            vec = embedder.encode(text, normalize_embeddings=True)
            return vec.tolist()
        # Fallback: random (non-functional, for dev only)
        return list(np.random.rand(384).astype(float))

    async def upsert_cluster(
        self,
        cluster_id: str,
        cluster: dict,
        board_id: str,
    ) -> bool:
        """Embed a peripheral cluster and store in Vectorize."""
        text = (
            f"Board {board_id}: "
            f"{cluster.get('dominant_theme', '')} — "
            f"{cluster['count']} shapes at {cluster['coords']}"
        )
        vector = await asyncio.to_thread(self._embed, text)

        payload = {
            "vectors": [{
                "id": cluster_id,
                "values": vector,
                "metadata": {
                    "board_id": board_id,
                    "theme": cluster.get("dominant_theme", ""),
                    "count": cluster["count"],
                    "coords": json.dumps(cluster["coords"]),
                    "types": json.dumps(cluster.get("types", [])),
                    "timestamp": int(time.time()),
                },
            }]
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self._base_url}/upsert",
                    headers=self._headers,
                    json=payload,
                )
                resp.raise_for_status()
                return True
        except Exception as e:
            logger.error("Vectorize upsert failed: %s", e)
            return False

    async def query(
        self,
        natural_query: str,
        board_id: str,
        top_k: int = 5,
    ) -> list[dict]:
        """Semantic search over stored spatial clusters."""
        vector = await asyncio.to_thread(self._embed, natural_query)

        payload = {
            "vector": vector,
            "topK": top_k,
            "filter": {"board_id": board_id},
            "returnMetadata": "all",
        }

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self._base_url}/query",
                    headers=self._headers,
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                results = data.get("result", {}).get("matches", [])
                return [
                    {
                        "cluster_id": r["id"],
                        "score": r["score"],
                        "theme": r.get("metadata", {}).get("theme"),
                        "count": r.get("metadata", {}).get("count"),
                        "coords": json.loads(r.get("metadata", {}).get("coords", "{}")),
                    }
                    for r in results
                ]
        except Exception as e:
            logger.error("Vectorize query failed: %s", e)
            return []





class PivotDetector:
    """
    Monitors participant cursor positions.
    If a user moves > pivot_distance_threshold_px in one update,
    emits a pivot event via asyncio.Event so running tasks can be cancelled.
    """

    def __init__(self) -> None:
        self._positions: dict[str, tuple[float, float]] = {}
        self.pivot_event = asyncio.Event()
        self._threshold = spatial_config.pivot_distance_threshold_px

    def update_position(self, participant_id: str, x: float, y: float) -> bool:
        """Returns True if a pivot was detected."""
        prev = self._positions.get(participant_id)
        self._positions[participant_id] = (x, y)

        if prev is None:
            return False

        dist = math.hypot(x - prev[0], y - prev[1])
        if dist > self._threshold:
            logger.info(
                "Pivot detected: %s moved %.0fpx (threshold=%.0fpx)",
                participant_id, dist, self._threshold,
            )
            self.pivot_event.set()
            return True
        return False

    def clear_pivot(self) -> None:
        """Reset the pivot event after handlers have responded."""
        self.pivot_event.clear()
