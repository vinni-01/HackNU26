"""
agent/tools.py — Gemini function-calling tool definitions and async implementations.

Tools in this module:
  1. generate_multimedia        — Higgsfield async pipeline (Soul/Cinematic/Image)
  2. execute_python_code        — Sandboxed Python for analysis and morphing
  3. check_canvas_occupancy     — R-Tree spatial query before placement
  4. place_shape_on_canvas      — Ghost Shape placement with feedback mitigation
  5. query_spatial_memory       — Cloudflare Vectorize semantic recall
  6. revert_to_previous_state   — Time-travel board revert via R2
  7. request_permission         — PERMISSION MODE ONLY: ask users before editing
  8. get_canvas_perception      — Return current tiered perception summary

AGENT MODE BEHAVIOUR:
  - "autonomous"  : the agent may call place_shape_on_canvas and generate_multimedia
                    directly without human approval.
  - "permission"  : the agent MUST call request_permission first. It may only proceed
                    with an edit after receiving an explicit approval event from the
                    canvas relay (approval stored in Redis at key
                    approval:{board_id}:{request_id}).

Every tool handler MUST return the incoming thought_signature verbatim.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import uuid
from typing import Any, Optional

import boto3
import httpx
import redis.asyncio as aioredis

from agent.config import env, spatial_config

logger = logging.getLogger(__name__)


_rtree: Optional[Any] = None
_spatial_rag: Optional[Any] = None
_snapshot_manager: Optional[Any] = None
_redis_client: Optional[aioredis.Redis] = None
_livekit_room_name: Optional[str] = None
_perception_loop: Optional[Any] = None   # PerceptionLoop instance

# Forbidden modules for sandboxed code execution
_BLOCKED_IMPORTS = frozenset({
    "os", "sys", "subprocess", "socket", "shutil", "pathlib",
    "importlib", "ctypes", "pickle", "shelve", "multiprocessing",
    "threading", "concurrent", "signal", "resource", "fcntl",
    "pwd", "grp", "pty", "tty", "termios",
})


def init_tools(
    rtree: Any,
    rag: Any,
    snapshot_mgr: Any,
    redis_client: aioredis.Redis,
    room_name: str,
    perception_loop: Optional[Any] = None,
) -> None:
    """Called once at agent startup to inject shared dependencies."""
    global _rtree, _spatial_rag, _snapshot_manager, _redis_client
    global _livekit_room_name, _perception_loop
    _rtree = rtree
    _spatial_rag = rag
    _snapshot_manager = snapshot_mgr
    _redis_client = redis_client
    _livekit_room_name = room_name
    _perception_loop = perception_loop



_r2_client: Any = None


def _get_r2() -> Any:
    global _r2_client
    if _r2_client is None:
        _r2_client = boto3.client(
            "s3",
            endpoint_url=env.r2_endpoint,
            aws_access_key_id=env.r2_access_key,
            aws_secret_access_key=env.r2_secret_key,
            region_name="auto",
        )
    return _r2_client



def _build_response(data: dict, thought_signature: Optional[str]) -> dict:
    """
    Wrap a tool result with the echoed thought_signature and a timestamp.
    Gemini MUST receive the signature back on every turn to preserve its
    internal reasoning chain and avoid 400 validation errors.
    """
    return {
        **data,
        "thought_signature": thought_signature,
        "timestamp": int(time.time()),
    }



async def _get_agent_mode(board_id: str) -> str:
    """Read the current agent_mode from Redis (set by the boards PATCH endpoint)."""
    if not _redis_client:
        return "permission"
    try:
        mode = await _redis_client.get(f"agent_mode:{board_id}")
        return mode if mode in ("autonomous", "permission") else "permission"
    except Exception:
        return "permission"


async def _check_approval(board_id: str, request_id: str, timeout: float = 30.0) -> bool:
    """
    Poll Redis for an approval at key  approval:{board_id}:{request_id}.
    Returns True if approved within timeout, False otherwise.
    The canvas relay writes this key when the user clicks Accept on a permission request.
    """
    if not _redis_client:
        return False
    deadline = time.time() + timeout
    while time.time() < deadline:
        val = await _redis_client.get(f"approval:{board_id}:{request_id}")
        if val == "approved":
            await _redis_client.delete(f"approval:{board_id}:{request_id}")
            return True
        if val == "rejected":
            await _redis_client.delete(f"approval:{board_id}:{request_id}")
            return False
        await asyncio.sleep(1.0)
    return False  # timeout — treat as rejection



GENERATE_MULTIMEDIA_DECLARATION = {
    "name": "generate_multimedia",
    "description": (
        "Generate a cinematic video or image using the Higgsfield AI engine. "
        "In AUTONOMOUS mode the job is enqueued immediately. "
        "In PERMISSION mode the agent first presents the intent to the room and "
        "waits for explicit human approval before enqueuing. "
        "Soul Mode is activated automatically when reference_image_urls is provided. "
        "Generation takes 1-3 minutes; the result arrives via webhook."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Detailed description of the video or image to generate.",
            },
            "media_type": {
                "type": "string",
                "enum": ["video_soul", "video_cinematic", "image"],
                "description": (
                    "video_soul = character-consistent Soul Mode (requires reference_image_urls), "
                    "video_cinematic = cinematic storyboard, "
                    "image = static frame."
                ),
            },
            "board_id": {
                "type": "string",
                "description": "The tldraw board ID where the Ghost Shape will appear.",
            },
            "reference_image_urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional R2 public URLs of reference character images. "
                    "Providing these activates Soul Mode for character-consistent generation."
                ),
            },
            "custom_reference_id": {
                "type": "string",
                "description": "Optional Higgsfield Soul ID for a pre-trained digital persona.",
            },
            "target_bounds": {
                "type": "object",
                "description": "Preferred canvas placement in Gemini [0-1000] space.",
                "properties": {
                    "y_min": {"type": "integer"},
                    "x_min": {"type": "integer"},
                    "y_max": {"type": "integer"},
                    "x_max": {"type": "integer"},
                },
            },
            "thought_signature": {
                "type": "string",
                "description": "The thoughtSignature from the previous tool response. REQUIRED.",
            },
        },
        "required": ["prompt", "media_type", "board_id", "thought_signature"],
    },
}


async def generate_multimedia(
    prompt: str,
    media_type: str,
    board_id: str,
    thought_signature: str,
    reference_image_urls: Optional[list[str]] = None,
    custom_reference_id: Optional[str] = None,
    target_bounds: Optional[dict] = None,
) -> dict:
    mode = await _get_agent_mode(board_id)

    if mode == "permission":
        request_id = uuid.uuid4().hex
        permission_event = {
            "type": "permission_request",
            "request_id": request_id,
            "action": "generate_multimedia",
            "description": (
                f"I would like to generate a {media_type} based on the following prompt: "
                f"\"{prompt[:200]}\". May I proceed?"
            ),
            "board_id": board_id,
            "source": "agent",
        }
        if _redis_client:
            await _redis_client.publish(
                f"canvas:events:{board_id}", json.dumps(permission_event)
            )

        approved = await _check_approval(board_id, request_id, timeout=30.0)
        if not approved:
            return _build_response({
                "status": "rejected",
                "message": "Media generation was not approved by the room.",
            }, thought_signature)

    job_id = f"hf_{uuid.uuid4().hex}"
    task_payload = {
        "job_id": job_id,
        "prompt": prompt,
        "media_type": media_type,
        "board_id": board_id,
        "reference_image_urls": reference_image_urls or [],
        "custom_reference_id": custom_reference_id,
        "target_bounds": target_bounds,
        "room_name": _livekit_room_name,
        "enqueued_at": int(time.time()),
    }

    if _redis_client:
        await _redis_client.lpush("bull:higgsfield:jobs", json.dumps(task_payload))
        logger.info("Enqueued Higgsfield job %s (type=%s)", job_id, media_type)
    else:
        logger.warning("Redis unavailable — Higgsfield job %s not enqueued", job_id)

    soul_note = " Soul Mode active (character-consistent)." if reference_image_urls else ""
    return _build_response({
        "status": "enqueued",
        "job_id": job_id,
        "message": (
            f"Higgsfield {media_type} job enqueued.{soul_note} "
            "I will narrate when the asset is ready on the canvas."
        ),
    }, thought_signature)



EXECUTE_PYTHON_DECLARATION = {
    "name": "execute_python_code",
    "description": (
        "Execute a Python script in a sandboxed environment for data analysis, "
        "similarity scoring, or structural morphing (arranging notes into grids or flowcharts). "
        "The script cannot access the network, filesystem, or environment variables."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "script": {
                "type": "string",
                "description": "Self-contained Python script to execute.",
            },
            "timeout_seconds": {
                "type": "integer",
                "description": "Max execution time in seconds (default 10, max 30).",
                "default": 10,
            },
            "thought_signature": {
                "type": "string",
                "description": "The thoughtSignature from the previous response. REQUIRED.",
            },
        },
        "required": ["script", "thought_signature"],
    },
}


async def execute_python_code(
    script: str,
    thought_signature: str,
    timeout_seconds: int = 10,
) -> dict:
    timeout_seconds = min(timeout_seconds, 30)

    for blocked in _BLOCKED_IMPORTS:
        if f"import {blocked}" in script or f"from {blocked}" in script:
            return _build_response({
                "status": "rejected",
                "error": f"Forbidden import detected: '{blocked}'.",
                "output": None,
            }, thought_signature)

    safe_script = textwrap.dedent(f"""
import sys
import builtins

_BLOCKED = {set(_BLOCKED_IMPORTS)!r}
_original_import = builtins.__import__

def _safe_import(name, *args, **kwargs):
    base = name.split('.')[0]
    if base in _BLOCKED:
        raise ImportError(f"Blocked import: {{name}}")
    return _original_import(name, *args, **kwargs)

builtins.__import__ = _safe_import

import io, contextlib
_stdout = io.StringIO()
with contextlib.redirect_stdout(_stdout), contextlib.redirect_stderr(_stdout):
    try:
{textwrap.indent(script, '        ')}
    except Exception as _e:
        print(f"RUNTIME ERROR: {{_e}}")

print(_stdout.getvalue())
""").strip()

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, prefix="agent_exec_"
    ) as f:
        f.write(safe_script)
        tmp_path = f.name

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_run_subprocess, tmp_path, timeout_seconds),
            timeout=timeout_seconds + 2,
        )
    except asyncio.TimeoutError:
        result = {"status": "timeout", "output": None, "error": "Execution timed out."}
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    return _build_response(result, thought_signature)


def _run_subprocess(script_path: str, timeout: int) -> dict:
    try:
        proc = subprocess.run(
            [sys.executable, script_path],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={},
            preexec_fn=_set_resource_limits if sys.platform != "win32" else None,
        )
        output = (proc.stdout + proc.stderr).strip()
        return {
            "status": "success" if proc.returncode == 0 else "error",
            "output": output[:4000],
            "exit_code": proc.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "output": None, "error": "Subprocess timed out."}
    except Exception as exc:
        return {"status": "error", "output": None, "error": str(exc)}


def _set_resource_limits() -> None:
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (15, 15))
        resource.setrlimit(resource.RLIMIT_AS, (256 * 1024 * 1024, 256 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (0, 0))
    except Exception:
        pass



CHECK_OCCUPANCY_DECLARATION = {
    "name": "check_canvas_occupancy",
    "description": (
        "Query the R-Tree spatial index to check whether a target area on the canvas "
        "is already occupied by human content. Always call this before place_shape_on_canvas."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "bounds": {
                "type": "object",
                "description": "Target bounds in Gemini [0-1000] space.",
                "properties": {
                    "y_min": {"type": "integer"},
                    "x_min": {"type": "integer"},
                    "y_max": {"type": "integer"},
                    "x_max": {"type": "integer"},
                },
                "required": ["y_min", "x_min", "y_max", "x_max"],
            },
            "canvas_width": {"type": "number"},
            "canvas_height": {"type": "number"},
            "thought_signature": {
                "type": "string",
                "description": "The thoughtSignature from the previous response. REQUIRED.",
            },
        },
        "required": ["bounds", "canvas_width", "canvas_height", "thought_signature"],
    },
}


async def check_canvas_occupancy(
    bounds: dict,
    canvas_width: float,
    canvas_height: float,
    thought_signature: str,
) -> dict:
    from agent.spatial_utils import gemini_to_canvas, normalize_to_gemini

    if not _rtree:
        return _build_response({"occupied": False, "conflicts": []}, thought_signature)

    pixel_bounds = gemini_to_canvas(bounds, canvas_width, canvas_height)
    conflicts = await _rtree.query(pixel_bounds, buffer=spatial_config.safety_buffer_px)

    if conflicts:
        alt = await _rtree.find_open_area(pixel_bounds)
        alt_gemini = normalize_to_gemini(alt, canvas_width, canvas_height)
        return _build_response({
            "occupied": True,
            "conflicts": conflicts,
            "suggested_alternative": alt_gemini,
        }, thought_signature)

    return _build_response({
        "occupied": False,
        "conflicts": [],
        "suggested_alternative": bounds,
    }, thought_signature)



PLACE_SHAPE_DECLARATION = {
    "name": "place_shape_on_canvas",
    "description": (
        "Place a Ghost Shape (opacity 0.4) on the tldraw canvas. "
        "In AUTONOMOUS mode, placement is immediate. "
        "In PERMISSION mode, this tool publishes a permission_request event and waits "
        "for the room to approve before placing. "
        "Always call check_canvas_occupancy beforehand."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "shape_type": {
                "type": "string",
                "enum": ["note", "text", "geo", "image", "arrow"],
            },
            "text": {"type": "string"},
            "bounds": {
                "type": "object",
                "properties": {
                    "y_min": {"type": "integer"},
                    "x_min": {"type": "integer"},
                    "y_max": {"type": "integer"},
                    "x_max": {"type": "integer"},
                },
            },
            "canvas_width": {"type": "number"},
            "canvas_height": {"type": "number"},
            "board_id": {"type": "string"},
            "color": {
                "type": "string",
                "description": "tldraw color token, e.g. 'violet', 'sky', 'light-green'.",
                "default": "violet",
            },
            "thought_signature": {
                "type": "string",
                "description": "The thoughtSignature from the previous response. REQUIRED.",
            },
        },
        "required": [
            "shape_type", "text", "bounds", "canvas_width",
            "canvas_height", "board_id", "thought_signature",
        ],
    },
}


async def place_shape_on_canvas(
    shape_type: str,
    text: str,
    bounds: dict,
    canvas_width: float,
    canvas_height: float,
    board_id: str,
    thought_signature: str,
    color: str = "violet",
) -> dict:
    from agent.spatial_utils import gemini_to_canvas, normalize_to_gemini

    mode = await _get_agent_mode(board_id)

    if mode == "permission":
        request_id = uuid.uuid4().hex
        permission_event = {
            "type": "permission_request",
            "request_id": request_id,
            "action": "place_shape",
            "description": (
                f"I would like to add a {shape_type} shape with the text: "
                f"\"{text[:150]}\". May I place it on the canvas?"
            ),
            "board_id": board_id,
            "source": "agent",
        }
        if _redis_client:
            await _redis_client.publish(
                f"canvas:events:{board_id}", json.dumps(permission_event)
            )

        approved = await _check_approval(board_id, request_id, timeout=30.0)
        if not approved:
            return _build_response({
                "status": "rejected",
                "message": "Shape placement was not approved by the room.",
            }, thought_signature)

    pixel_bounds = gemini_to_canvas(bounds, canvas_width, canvas_height)

    if _rtree and await _rtree.is_occupied(pixel_bounds):
        pixel_bounds = await _rtree.find_open_area(pixel_bounds)
        logger.info("Shape relocated to avoid collision at %s", pixel_bounds)

    final_gemini = normalize_to_gemini(pixel_bounds, canvas_width, canvas_height)
    shape_id = f"shape:agent_{uuid.uuid4().hex[:8]}"

    shape_payload = {
        "id": shape_id,
        "type": shape_type,
        "x": pixel_bounds.x,
        "y": pixel_bounds.y,
        "props": {
            "text": text,
            "color": color,
            "w": pixel_bounds.w,
            "h": pixel_bounds.h,
        },
        "meta": {
            "source": "agent",
            "opacity": spatial_config.ghost_opacity_default,
            "ghost": True,
            "accept_action": "agent:accept_shape",
            "reject_action": "agent:reject_shape",
        },
        "opacity": spatial_config.ghost_opacity_default,
    }

    if _redis_client:
        await _redis_client.publish(
            f"canvas:events:{board_id}",
            json.dumps({"type": "agent_shape", "shape": shape_payload, "board_id": board_id}),
        )

    if _rtree:
        await _rtree.insert(shape_id, pixel_bounds)

    return _build_response({
        "status": "placed",
        "shape_id": shape_id,
        "final_coords": final_gemini,
        "opacity": spatial_config.ghost_opacity_default,
        "message": f"Ghost Shape placed at {final_gemini}. Awaiting human accept or reject.",
    }, thought_signature)



REQUEST_PERMISSION_DECLARATION = {
    "name": "request_permission",
    "description": (
        "PERMISSION MODE ONLY. Publish a structured permission request to the canvas room "
        "and wait for a human Accept or Reject response. "
        "Use this before any edit when agent_mode is 'permission'. "
        "Returns whether the request was approved."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action_description": {
                "type": "string",
                "description": (
                    "A clear, concise description of what the agent intends to do. "
                    "This is displayed directly to the human participants."
                ),
            },
            "action_type": {
                "type": "string",
                "enum": ["place_shape", "generate_multimedia", "run_code", "revert_state", "other"],
            },
            "board_id": {"type": "string"},
            "timeout_seconds": {
                "type": "integer",
                "description": "How long to wait for approval (default 30, max 120).",
                "default": 30,
            },
            "thought_signature": {
                "type": "string",
                "description": "The thoughtSignature from the previous response. REQUIRED.",
            },
        },
        "required": ["action_description", "action_type", "board_id", "thought_signature"],
    },
}


async def request_permission(
    action_description: str,
    action_type: str,
    board_id: str,
    thought_signature: str,
    timeout_seconds: int = 30,
) -> dict:
    timeout_seconds = min(timeout_seconds, 120)
    request_id = uuid.uuid4().hex

    permission_event = {
        "type": "permission_request",
        "request_id": request_id,
        "action_type": action_type,
        "description": action_description,
        "board_id": board_id,
        "source": "agent",
    }

    if _redis_client:
        await _redis_client.publish(
            f"canvas:events:{board_id}", json.dumps(permission_event)
        )
        logger.info(
            "Permission request %s published (action=%s)", request_id, action_type
        )

    approved = await _check_approval(board_id, request_id, timeout=float(timeout_seconds))

    return _build_response({
        "approved": approved,
        "request_id": request_id,
        "message": "Approved by the room." if approved else "Not approved within the timeout.",
    }, thought_signature)



QUERY_MEMORY_DECLARATION = {
    "name": "query_spatial_memory",
    "description": (
        "Semantic search over archived spatial clusters in Cloudflare Vectorize. "
        "Use this to recall 'that cluster of revenue ideas in the top-right' or "
        "'the onboarding discussion from earlier in the session'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "board_id": {"type": "string"},
            "top_k": {"type": "integer", "default": 5},
            "thought_signature": {
                "type": "string",
                "description": "The thoughtSignature from the previous response. REQUIRED.",
            },
        },
        "required": ["query", "board_id", "thought_signature"],
    },
}


async def query_spatial_memory(
    query: str,
    board_id: str,
    thought_signature: str,
    top_k: int = 5,
) -> dict:
    if not _spatial_rag:
        return _build_response({"results": [], "error": "RAG not initialized"}, thought_signature)
    results = await _spatial_rag.query(query, board_id, top_k=top_k)
    return _build_response({"results": results, "query": query, "count": len(results)}, thought_signature)



REVERT_STATE_DECLARATION = {
    "name": "revert_to_previous_state",
    "description": (
        "Fetch an earlier tldraw board snapshot from R2 storage and broadcast it "
        "to all canvas clients. Use only when explicitly requested by users. "
        "In PERMISSION mode, requires human approval before executing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "snapshot_id": {"type": "string"},
            "board_id": {"type": "string"},
            "thought_signature": {
                "type": "string",
                "description": "The thoughtSignature from the previous response. REQUIRED.",
            },
        },
        "required": ["snapshot_id", "board_id", "thought_signature"],
    },
}


async def revert_to_previous_state(
    snapshot_id: str,
    board_id: str,
    thought_signature: str,
) -> dict:
    mode = await _get_agent_mode(board_id)

    if mode == "permission":
        request_id = uuid.uuid4().hex
        permission_event = {
            "type": "permission_request",
            "request_id": request_id,
            "action": "revert_state",
            "description": (
                f"I would like to revert the board to snapshot {snapshot_id}. "
                "This will replace the current canvas state. May I proceed?"
            ),
            "board_id": board_id,
            "source": "agent",
        }
        if _redis_client:
            await _redis_client.publish(
                f"canvas:events:{board_id}", json.dumps(permission_event)
            )
        approved = await _check_approval(board_id, request_id, timeout=30.0)
        if not approved:
            return _build_response({
                "status": "rejected",
                "message": "Board revert was not approved.",
            }, thought_signature)

    if not _snapshot_manager:
        return _build_response(
            {"status": "error", "message": "Snapshot manager not initialized"},
            thought_signature,
        )

    state = await _snapshot_manager.fetch_snapshot(snapshot_id)
    if not state:
        return _build_response(
            {"status": "error", "message": f"Snapshot {snapshot_id} not found"},
            thought_signature,
        )

    if _redis_client:
        await _redis_client.publish(
            f"canvas:events:{board_id}",
            json.dumps({
                "type": "revert",
                "snapshot_id": snapshot_id,
                "state": state,
                "board_id": board_id,
                "source": "agent",
            }),
        )

    return _build_response({
        "status": "reverted",
        "snapshot_id": snapshot_id,
        "message": f"Board reverted to snapshot {snapshot_id}.",
    }, thought_signature)



GET_PERCEPTION_DECLARATION = {
    "name": "get_canvas_perception",
    "description": (
        "Retrieve the latest tiered canvas perception summary "
        "(FocusedShape / BlurryShape / PeripheralShapeCluster) "
        "from the background perception loop. Call this before making any spatial decision."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "board_id": {"type": "string"},
            "thought_signature": {
                "type": "string",
                "description": "The thoughtSignature from the previous response. REQUIRED.",
            },
        },
        "required": ["board_id", "thought_signature"],
    },
}


async def get_canvas_perception(
    board_id: str,
    thought_signature: str,
) -> dict:
    summary = "(perception loop not active)"
    if _perception_loop:
        summary = await _perception_loop.get_latest_summary()
    elif _redis_client:
        try:
            raw = await _redis_client.get(f"perception:summary:{board_id}")
            if raw:
                summary = raw
        except Exception:
            pass

    return _build_response({"perception": summary}, thought_signature)


# Tool Registry

TOOL_DECLARATIONS = [
    GENERATE_MULTIMEDIA_DECLARATION,
    EXECUTE_PYTHON_DECLARATION,
    CHECK_OCCUPANCY_DECLARATION,
    PLACE_SHAPE_DECLARATION,
    REQUEST_PERMISSION_DECLARATION,
    QUERY_MEMORY_DECLARATION,
    REVERT_STATE_DECLARATION,
    GET_PERCEPTION_DECLARATION,
]

TOOL_HANDLERS: dict[str, Any] = {
    "generate_multimedia":    generate_multimedia,
    "execute_python_code":    execute_python_code,
    "check_canvas_occupancy": check_canvas_occupancy,
    "place_shape_on_canvas":  place_shape_on_canvas,
    "request_permission":     request_permission,
    "query_spatial_memory":   query_spatial_memory,
    "revert_to_previous_state": revert_to_previous_state,
    "get_canvas_perception":  get_canvas_perception,
}
