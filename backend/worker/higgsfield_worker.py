"""
worker/higgsfield_worker.py — Async BullMQ-compatible Higgsfield generation worker.

Dequeues jobs from  bull:higgsfield:jobs  (Redis LIST, LPUSH/BRPOP).
For each job:
  1. Checks the pivot:{board_id} Redis key — if set, cancels the job immediately
     because the users have moved to a new canvas area.
  2. Calls the Higgsfield REST API to submit the generation request.
  3. Polls the job status until "completed" or "failed" (with exponential backoff).
  4. On completion, POSTs to the FastAPI webhook endpoint which handles R2 upload
     and LiveKit injection.
  5. On failure or cancellation, publishes an asset_failed event to canvas relay.

Soul Mode: activated whenever reference_image_urls is non-empty.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from typing import Optional

import httpx
import redis.asyncio as aioredis
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("worker.higgsfield")


REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
HIGGSFIELD_API_KEY = os.environ["HIGGSFIELD_API_KEY"]
HIGGSFIELD_BASE_URL = os.environ.get("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai/v1")
API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")
CF_WEBHOOK_SECRET = os.environ.get("CF_WEBHOOK_SECRET", "")

QUEUE_KEY = "bull:higgsfield:jobs"
POLL_INTERVAL_INITIAL = 10   # seconds
POLL_INTERVAL_MAX = 30       # seconds
POLL_TIMEOUT = 240           # 4-minute hard cap



class HiggsfieldClient:
    """Thin async wrapper around the Higgsfield REST API."""

    def __init__(self, api_key: str, base_url: str) -> None:
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._base = base_url.rstrip("/")

    async def submit_video_soul(
        self,
        prompt: str,
        reference_image_urls: list[str],
        custom_reference_id: Optional[str] = None,
    ) -> dict:
        """Submit a Soul Mode video generation request."""
        payload: dict = {"prompt": prompt, "reference_image_urls": reference_image_urls}
        if custom_reference_id:
            payload["custom_reference_id"] = custom_reference_id
        return await self._post("/videos/soul", payload)

    async def submit_video_cinematic(self, prompt: str) -> dict:
        """Submit a cinematic storyboard video generation request."""
        return await self._post("/videos/cinematic", {"prompt": prompt})

    async def submit_image(self, prompt: str) -> dict:
        """Submit a static image generation request."""
        return await self._post("/images/generate", {"prompt": prompt})

    async def get_status(self, generation_id: str, media_type: str) -> dict:
        """Poll a generation job for status."""
        path_map = {
            "video_soul": "/videos/soul",
            "video_cinematic": "/videos/cinematic",
            "image": "/images",
        }
        base_path = path_map.get(media_type, "/videos/cinematic")
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{self._base}{base_path}/{generation_id}",
                headers=self._headers,
            )
            resp.raise_for_status()
            return resp.json()

    async def _post(self, path: str, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self._base}{path}",
                headers=self._headers,
                json=payload,
            )
            resp.raise_for_status()
            return resp.json()



async def _is_pivot_active(redis: aioredis.Redis, board_id: str) -> bool:
    """Return True if a pivot has been flagged for this board."""
    val = await redis.get(f"pivot:{board_id}")
    return val is not None


async def _clear_pivot(redis: aioredis.Redis, board_id: str) -> None:
    await redis.delete(f"pivot:{board_id}")



async def _notify_webhook(payload: dict) -> None:
    """POST completion/failure result to the FastAPI webhook endpoint."""
    import hashlib
    import hmac as hmac_mod

    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if CF_WEBHOOK_SECRET:
        sig = "sha256=" + hmac_mod.new(
            CF_WEBHOOK_SECRET.encode(), body, hashlib.sha256
        ).hexdigest()
        headers["X-Higgsfield-Signature"] = sig

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{API_BASE_URL}/webhooks/higgsfield/complete",
                content=body,
                headers=headers,
            )
            resp.raise_for_status()
            logger.info("Webhook delivered for job %s", payload.get("job_id"))
    except Exception as exc:
        logger.error("Webhook delivery failed for job %s: %s", payload.get("job_id"), exc)



async def process_job(job: dict, redis: aioredis.Redis, hf: HiggsfieldClient) -> None:
    job_id = job["job_id"]
    board_id = job.get("board_id", "")
    media_type = job.get("media_type", "video_cinematic")
    prompt = job.get("prompt", "")
    reference_image_urls = job.get("reference_image_urls") or []
    custom_reference_id = job.get("custom_reference_id")
    room_name = job.get("room_name", board_id)
    target_bounds = job.get("target_bounds")

    logger.info("Processing job %s (type=%s, board=%s)", job_id, media_type, board_id)

    # Pivot check before submission.
    if await _is_pivot_active(redis, board_id):
        logger.info("Job %s cancelled: pivot detected for board %s", job_id, board_id)
        await _notify_webhook({
            "job_id": job_id,
            "status": "failed",
            "media_type": media_type,
            "board_id": board_id,
            "room_name": room_name,
            "error": "Cancelled: users pivoted to a new canvas area.",
        })
        return

    # Submit to Higgsfield.
    try:
        if media_type == "video_soul":
            response = await hf.submit_video_soul(
                prompt=prompt,
                reference_image_urls=reference_image_urls,
                custom_reference_id=custom_reference_id,
            )
        elif media_type == "video_cinematic":
            response = await hf.submit_video_cinematic(prompt=prompt)
        else:
            response = await hf.submit_image(prompt=prompt)

        generation_id = response.get("id") or response.get("generation_id")
        if not generation_id:
            raise ValueError(f"No generation_id in response: {response}")
        logger.info("Higgsfield generation submitted: %s -> %s", job_id, generation_id)
    except Exception as exc:
        logger.error("Higgsfield submission failed for job %s: %s", job_id, exc)
        await _notify_webhook({
            "job_id": job_id,
            "status": "failed",
            "media_type": media_type,
            "board_id": board_id,
            "room_name": room_name,
            "error": str(exc),
        })
        return

    # Poll until complete, with pivot-check on each cycle.
    interval = POLL_INTERVAL_INITIAL
    deadline = time.time() + POLL_TIMEOUT

    while time.time() < deadline:
        if await _is_pivot_active(redis, board_id):
            logger.info("Job %s cancelled mid-poll: pivot detected", job_id)
            await _notify_webhook({
                "job_id": job_id,
                "status": "failed",
                "media_type": media_type,
                "board_id": board_id,
                "room_name": room_name,
                "error": "Cancelled mid-generation: canvas pivot.",
            })
            return

        await asyncio.sleep(interval)
        interval = min(interval * 1.5, POLL_INTERVAL_MAX)

        try:
            status_resp = await hf.get_status(generation_id, media_type)
        except Exception as exc:
            logger.warning("Poll error for job %s: %s", job_id, exc)
            continue

        job_status = status_resp.get("status", "").lower()
        logger.debug("Job %s status: %s", job_id, job_status)

        if job_status in {"completed", "succeeded", "done"}:
            asset_url = (
                status_resp.get("url")
                or status_resp.get("output_url")
                or status_resp.get("video_url")
                or status_resp.get("image_url")
            )
            await _notify_webhook({
                "job_id": job_id,
                "status": "completed",
                "media_type": media_type,
                "asset_url": asset_url,
                "board_id": board_id,
                "room_name": room_name,
                "target_bounds": target_bounds,
            })
            return

        if job_status in {"failed", "error", "cancelled"}:
            await _notify_webhook({
                "job_id": job_id,
                "status": "failed",
                "media_type": media_type,
                "board_id": board_id,
                "room_name": room_name,
                "error": status_resp.get("error", "Generation failed"),
            })
            return

    # Timeout exceeded.
    logger.error("Job %s timed out after %ds", job_id, POLL_TIMEOUT)
    await _notify_webhook({
        "job_id": job_id,
        "status": "failed",
        "media_type": media_type,
        "board_id": board_id,
        "room_name": room_name,
        "error": f"Generation timed out after {POLL_TIMEOUT}s",
    })



async def run_worker() -> None:
    logger.info("Higgsfield worker starting. Queue: %s", QUEUE_KEY)
    redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    hf = HiggsfieldClient(api_key=HIGGSFIELD_API_KEY, base_url=HIGGSFIELD_BASE_URL)

    # Concurrency: process up to 3 jobs in parallel.
    semaphore = asyncio.Semaphore(3)
    active_tasks: set[asyncio.Task] = set()

    async def _bounded_process(job: dict) -> None:
        async with semaphore:
            await process_job(job, redis, hf)

    try:
        while True:
            # BRPOP blocks (up to 5 s) until a job is available.
            result = await redis.brpop(QUEUE_KEY, timeout=5)
            if result is None:
                continue  # timeout, loop again

            _, raw_job = result
            try:
                job = json.loads(raw_job)
            except json.JSONDecodeError as exc:
                logger.error("Invalid job payload: %s — %s", raw_job[:200], exc)
                continue

            task = asyncio.create_task(_bounded_process(job))
            active_tasks.add(task)
            task.add_done_callback(active_tasks.discard)

    except asyncio.CancelledError:
        logger.info("Worker cancelled — waiting for in-flight tasks")
        if active_tasks:
            await asyncio.gather(*active_tasks, return_exceptions=True)
    finally:
        await redis.aclose()
        logger.info("Worker shutdown complete")


def _handle_signal(loop: asyncio.AbstractEventLoop, task: asyncio.Task) -> None:
    logger.info("Received shutdown signal")
    task.cancel()


if __name__ == "__main__":
    loop = asyncio.new_event_loop()
    main_task = loop.create_task(run_worker())
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal, loop, main_task)
        except NotImplementedError:
            pass  # Windows does not support add_signal_handler
    try:
        loop.run_until_complete(main_task)
    finally:
        loop.close()
