"""
app/routers/files.py — File upload to Cloudflare R2.

POST /files/upload  — Upload any file and receive a permanent public R2 URL.
                      Requires authentication. Files are namespaced under
                      uploads/{user_id}/{filename} to avoid collisions.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
import uuid
from pathlib import Path
from typing import Optional

import boto3
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from app.core.config import settings
from app.deps import get_current_user
from app.models.user import User

logger = logging.getLogger("files.router")
router = APIRouter(prefix="/files", tags=["files"])

# Maximum allowed upload size: 50 MB
MAX_UPLOAD_BYTES = 50 * 1024 * 1024

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


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_file(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
) -> dict:
    """
    Upload a file to Cloudflare R2 and return its permanent public URL.
    The file is stored at  uploads/{user_id}/{uuid}_{original_filename}.
    Authentication is required.
    """
    if not file.filename:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing filename")

    # Read and enforce size limit
    contents = await file.read()
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large. Maximum size is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.",
        )

    # Sanitise filename and build a collision-safe R2 key
    safe_name = Path(file.filename).name  # strip any path traversal
    unique_prefix = uuid.uuid4().hex[:8]
    r2_key = f"uploads/{current_user.id}/{unique_prefix}_{safe_name}"

    # Infer content type
    content_type = (
        file.content_type
        or mimetypes.guess_type(safe_name)[0]
        or "application/octet-stream"
    )

    # Upload to R2 (run in thread to avoid blocking event loop)
    if not settings.r2_endpoint or not settings.r2_access_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="File storage is not configured on this server.",
        )

    try:
        r2 = _get_r2()
        await asyncio.to_thread(
            r2.put_object,
            Bucket=settings.r2_bucket,
            Key=r2_key,
            Body=contents,
            ContentType=content_type,
            ACL="public-read",
        )
    except Exception as exc:
        logger.error("R2 upload failed for user %s: %s", current_user.id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Upload to storage failed. Please try again.",
        )

    public_url = f"{settings.r2_public_base.rstrip('/')}/{r2_key}"
    logger.info("File uploaded: %s → %s", safe_name, public_url)

    return {
        "filename": safe_name,
        "content_type": content_type,
        "size_bytes": len(contents),
        "r2_key": r2_key,
        "url": public_url,
    }