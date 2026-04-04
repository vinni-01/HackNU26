"""
app/routers/webhook.py — Higgsfield completion webhook and LiveKit session injection.

Lifecycle:
  1. The Higgsfield worker enqueues a generation job and registers the job_id in Redis.
  2. When Higgsfield calls this endpoint with the finished asset URL, we:
       a. Download the asset and upload a permanent copy to Cloudflare R2.
       b. Broadcast a LiveKit data packet to the active session so the agent can
          narrate the asset's arrival without polling.
       c. Publish an agent_shape event to the canvas relay so the Ghost Shape
          renders in the correct tldraw room.
  3. HMAC validation (CF_WEBHOOK_SECRET) ensures only Higgsfield calls this endpoint.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Optional

import boto3
import httpx
import redis.asyncio as aioredis
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, status
from livekit.api import LiveKitAPI, SendDataRequest
from pydantic import BaseModel

from app.core.config import settings

logger = logging.getLogger("webhook.higgsfield")
router = APIRouter(prefix="/webhooks", tags=["webhooks"])



class Higgsfield_CompletionPayload(BaseModel):
    job_id: str
    status: str                      # "completed" | "failed"
    media_type: str
    asset_url: Optional[str] = None  # Higgsfield-hosted URL (expires after 24 h)
    board_id: Optional[str] = None
    room_name: Optional[str] = None
    target_bounds: Optional[dict] = None
    error: Optional[str] = None



_r2: Optional[boto3.client] = None

def _get_r2():
    global _r2
    if _r2 is None:
        _r2 = boto3.client(
            "s3",
            endpoint_url=settings.r2_endpoint,
            aws_access_key_id=settings.r2_access_key,
            aws_secret_access_key=settings.r2_secret_key,
            region_name="auto",
        )
    return _r2



def _verify_signature(body: bytes, signature: Optional[str]) -> None:
    """
    Validate the X-Higgsfield-Signature header using the shared webhook secret.
    Skip validation when no secret is configured (local development only).
    """
    secret = settings.cf_webhook_secret
    if not secret:
        return  # development mode — no validation
    if not signature:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing webhook signature",
        )
    expected = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook signature",
        )



@router.post("/higgsfield/complete", status_code=status.HTTP_202_ACCEPTED)
async def higgsfield_complete(
    request: Request,
    background_tasks: BackgroundTasks,
    x_higgsfield_signature: Optional[str] = Header(default=None),
) -> dict:
    """
    Called by the Higgsfield worker (or Higgsfield directly) upon job completion.
    Enqueues post-processing in the background so the HTTP response is immediate.
    """
    body = await request.body()
    _verify_signature(body, x_higgsfield_signature)

    try:
        payload = Higgsfield_CompletionPayload.model_validate_json(body)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid payload: {exc}",
        )

    if payload.status == "failed":
        logger.warning("Higgsfield job %s failed: %s", payload.job_id, payload.error)
        # Notify the canvas room that the job failed.
        background_tasks.add_task(_notify_failure, payload)
        return {"received": True, "status": "failed"}

    background_tasks.add_task(_process_completion, payload)
    return {"received": True, "status": "processing"}



async def _process_completion(payload: Higgsfield_CompletionPayload) -> None:
    """Download asset, upload to R2, inject into LiveKit, publish Ghost Shape."""
    if not payload.asset_url:
        logger.error("Completion payload for job %s has no asset_url", payload.job_id)
        return

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(payload.asset_url)
            resp.raise_for_status()
            asset_bytes = resp.content
            content_type = resp.headers.get("content-type", "video/mp4")
    except Exception as exc:
        logger.error("Asset download failed for job %s: %s", payload.job_id, exc)
        return

    ext = "mp4" if "video" in content_type else "jpg"
    r2_key = f"media/{payload.board_id or 'unknown'}/{payload.job_id}.{ext}"
    public_url = f"{settings.r2_public_base}/{r2_key}"

    try:
        r2 = _get_r2()
        import asyncio
        await asyncio.to_thread(
            r2.put_object,
            Bucket=settings.r2_bucket,
            Key=r2_key,
            Body=asset_bytes,
            ContentType=content_type,
            ACL="public-read",
        )
        logger.info("Asset uploaded to R2: %s", r2_key)
    except Exception as exc:
        logger.error("R2 upload failed for job %s: %s", payload.job_id, exc)
        return

    shape_id = f"shape:agent_{uuid.uuid4().hex[:8]}"
    bounds = payload.target_bounds or {"y_min": 400, "x_min": 400, "y_max": 600, "x_max": 700}

    canvas_event = {
        "type": "agent_shape",
        "board_id": payload.board_id,
        "shape": {
            "id": shape_id,
            "type": "image",
            "props": {
                "url": public_url,
                "w": 400,
                "h": 300,
            },
            "meta": {
                "source": "agent",
                "ghost": True,
                "opacity": 0.4,
                "job_id": payload.job_id,
                "accept_action": "agent:accept_shape",
                "reject_action": "agent:reject_shape",
            },
            "opacity": 0.4,
        },
        "target_bounds": bounds,
    }

    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        room = payload.room_name or payload.board_id or ""
        await redis_client.publish(f"canvas:events:{room}", json.dumps(canvas_event))

        await _inject_livekit_message(
            room_name=room,
            message={
                "type": "asset_ready",
                "job_id": payload.job_id,
                "media_type": payload.media_type,
                "public_url": public_url,
                "shape_id": shape_id,
                "message": (
                    f"The {payload.media_type} asset for job {payload.job_id} "
                    "is now on the canvas. I have placed it as a Ghost Shape "
                    "so you can review it before accepting."
                ),
            },
        )
    finally:
        await redis_client.aclose()


async def _notify_failure(payload: Higgsfield_CompletionPayload) -> None:
    room = payload.room_name or payload.board_id or ""
    if not room:
        return
    redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        await redis_client.publish(
            f"canvas:events:{room}",
            json.dumps({
                "type": "asset_failed",
                "job_id": payload.job_id,
                "error": payload.error,
                "source": "agent",
            }),
        )
    finally:
        await redis_client.aclose()


async def _inject_livekit_message(room_name: str, message: dict) -> None:
    """
    Send a LiveKit data packet to all participants in the room.
    The agent's session receives this as a data message and narrates the content.
    """
    if not room_name:
        return
    try:
        lk_api = LiveKitAPI(
            url=settings.livekit_url,
            api_key=settings.livekit_api_key,
            api_secret=settings.livekit_api_secret,
        )
        await lk_api.room.send_data(
            SendDataRequest(
                room=room_name,
                data=json.dumps(message).encode(),
                reliable=True,
            )
        )
        await lk_api.aclose()
        logger.info("LiveKit data injected into room %s (type=%s)", room_name, message.get("type"))
    except Exception as exc:
        logger.error("LiveKit injection failed for room %s: %s", room_name, exc)
