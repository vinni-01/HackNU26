"""
app/routers/memory.py — REST endpoints for Spatial RAG and board snapshot history.

Endpoints:
  GET  /memory/snapshots/{board_id}          list available snapshot keys from R2
  POST /memory/snapshots/{board_id}/take     trigger an immediate snapshot
  GET  /memory/clusters/{board_id}           query Cloudflare Vectorize by natural language
  POST /memory/clusters/{board_id}           upsert a spatial cluster embedding
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

import boto3
import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.core.config import settings
from app.deps import get_current_user
from app.models.user import User

logger = logging.getLogger("memory.router")
router = APIRouter(prefix="/memory", tags=["memory"])


# R2 client 

def _r2():
    return boto3.client(
        "s3",
        endpoint_url=settings.r2_endpoint,
        aws_access_key_id=settings.r2_access_key,
        aws_secret_access_key=settings.r2_secret_key,
        region_name="auto",
    )


# Pydantic schemas 

class ClusterUpsertRequest(BaseModel):
    cluster_id: str
    dominant_theme: str
    count: int
    coords: dict        # Gemini [y_min, x_min, y_max, x_max] in 0-1000 space
    types: list[str] = []


class ClusterQueryResponse(BaseModel):
    cluster_id: str
    score: float
    theme: Optional[str]
    count: Optional[int]
    coords: dict


#  Snapshot endpoints 

@router.get("/snapshots/{board_id}")
async def list_snapshots(
    board_id: str,
    limit: int = Query(default=20, le=100),
    current_user: User = Depends(get_current_user),
) -> dict:
    """
    List the most recent snapshot keys for a board, sorted by timestamp descending.
    Each key can be passed to the agent's revert_to_previous_state tool.
    """
    prefix = f"snapshots/{board_id}/"
    try:
        r2 = _r2()
        response = await asyncio.to_thread(
            r2.list_objects_v2,
            Bucket=settings.r2_bucket,
            Prefix=prefix,
            MaxKeys=limit,
        )
        objects = response.get("Contents", [])
        snapshots = [
            {
                "snapshot_id": obj["Key"].replace("snapshots/", "").replace(".json", ""),
                "key": obj["Key"],
                "size_bytes": obj["Size"],
                "last_modified": obj["LastModified"].isoformat(),
            }
            for obj in sorted(objects, key=lambda o: o["LastModified"], reverse=True)
        ]
        return {"board_id": board_id, "snapshots": snapshots, "total": len(snapshots)}
    except Exception as exc:
        logger.error("Snapshot list failed for board %s: %s", board_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not list snapshots from R2",
        )


@router.post("/snapshots/{board_id}/take", status_code=status.HTTP_202_ACCEPTED)
async def trigger_snapshot(
    board_id: str,
    current_user: User = Depends(get_current_user),
) -> dict:
    """
    Publish a snapshot_request event to Redis so the agent's SnapshotManager
    takes an immediate out-of-schedule snapshot.
    """
    redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        await redis.publish(
            f"canvas:events:{board_id}",
            json.dumps({"type": "snapshot_request", "source": "api", "board_id": board_id}),
        )
    finally:
        await redis.aclose()
    return {"status": "snapshot_requested", "board_id": board_id}


#  Vectorize cluster endpoints 

@router.get("/clusters/{board_id}", response_model=list[ClusterQueryResponse])
async def query_clusters(
    board_id: str,
    q: str = Query(..., description="Natural language spatial query"),
    top_k: int = Query(default=5, le=20),
    current_user: User = Depends(get_current_user),
) -> list[ClusterQueryResponse]:
    """
    Semantic search over archived spatial clusters for this board.
    Delegates to Cloudflare Vectorize via the SpatialRAG helper.
    """
    from agent.spatial_utils import SpatialRAG
    rag = SpatialRAG()
    results = await rag.query(natural_query=q, board_id=board_id, top_k=top_k)
    return [
        ClusterQueryResponse(
            cluster_id=r["cluster_id"],
            score=r["score"],
            theme=r.get("theme"),
            count=r.get("count"),
            coords=r.get("coords") or {},
        )
        for r in results
    ]


@router.post("/clusters/{board_id}", status_code=status.HTTP_201_CREATED)
async def upsert_cluster(
    board_id: str,
    body: ClusterUpsertRequest,
    current_user: User = Depends(get_current_user),
) -> dict:
    """
    Manually upsert a spatial cluster embedding into Vectorize.
    Typically called by the agent's perception loop, but exposed here
    for administrative use (e.g., seeding from an existing board export).
    """
    from agent.spatial_utils import SpatialRAG
    rag = SpatialRAG()
    success = await rag.upsert_cluster(
        cluster_id=body.cluster_id,
        cluster={
            "dominant_theme": body.dominant_theme,
            "count": body.count,
            "coords": body.coords,
            "types": body.types,
        },
        board_id=board_id,
    )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Vectorize upsert failed",
        )
    return {"status": "upserted", "cluster_id": body.cluster_id}
