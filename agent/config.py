"""
config.py - Agent persona, Gemini caching config, and all environment settings.

All secrets are read from environment variables (or .env via python-dotenv).
Nothing sensitive is hardcoded here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()





@dataclass(frozen=True)
class EnvConfig:
    # LiveKit
    livekit_url: str = field(default_factory=lambda: os.environ["LIVEKIT_URL"])
    livekit_api_key: str = field(default_factory=lambda: os.environ["LIVEKIT_API_KEY"])
    livekit_api_secret: str = field(default_factory=lambda: os.environ["LIVEKIT_API_SECRET"])

    # Gemini
    gemini_api_key: str = field(default_factory=lambda: os.environ["GEMINI_API_KEY"])
    gemini_model: str = field(default_factory=lambda: os.getenv(
        "GEMINI_MODEL", "gemini-2.0-flash-live-001"
    ))

    # Higgsfield
    higgsfield_api_key: str = field(default_factory=lambda: os.environ["HIGGSFIELD_API_KEY"])
    higgsfield_base_url: str = field(default_factory=lambda: os.getenv(
        "HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai/v1"
    ))

    # Cloudflare R2
    r2_endpoint: str = field(default_factory=lambda: os.environ["R2_ENDPOINT"])
    r2_access_key: str = field(default_factory=lambda: os.environ["R2_ACCESS_KEY"])
    r2_secret_key: str = field(default_factory=lambda: os.environ["R2_SECRET_KEY"])
    r2_bucket: str = field(default_factory=lambda: os.getenv("R2_BUCKET", "brainstorm-assets"))
    r2_public_base: str = field(default_factory=lambda: os.environ["R2_PUBLIC_BASE"])

    # Cloudflare Vectorize
    vectorize_account_id: str = field(default_factory=lambda: os.environ["CF_ACCOUNT_ID"])
    vectorize_api_token: str = field(default_factory=lambda: os.environ["CF_API_TOKEN"])
    vectorize_index: str = field(default_factory=lambda: os.getenv(
        "VECTORIZE_INDEX", "spatial-rag-index"
    ))

    # Redis / BullMQ
    redis_url: str = field(default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379"))

    # FastAPI backend (for internal webhook calls)
    api_base_url: str = field(default_factory=lambda: os.getenv(
        "API_BASE_URL", "http://localhost:8000"
    ))


# Singleton
env = EnvConfig()



# Context & Caching Configuration


@dataclass
class CachingConfig:
    """
    Implicit caching triggers on turns > 2,048 tokens.
    Explicit caching is used for the system instruction (persona prompt)
    which is large and static - avoids re-billing on every turn.

    Audio produces ~25 tokens/sec → ~1,500 tokens/min per participant.
    At 3 participants (2 humans + AI) over 30 min ≈ 135,000 tokens
    before compression kicks in.
    """
    implicit_cache_threshold_tokens: int = 2_048
    # Explicit cache TTL for system instruction (in seconds)
    system_instruction_cache_ttl: int = 60 * 60 * 4   # 4 hours

    # Context window compression
    # Summarize when window exceeds ~80% of model capacity
    compression_trigger_tokens: int = 900_000
    # Target size after compression
    compression_target_tokens: int = 200_000

    # Snapshot interval for time-travel replay (seconds)
    snapshot_interval_seconds: int = 60


caching_config = CachingConfig()


# Personas & Instructions


BRAINSTORM_PERSONA = """
You are **Muse**, an AI spatial collaborator embedded inside a shared brainstorm canvas.
You exist as a peer alongside the human participants - not as an assistant that responds to
commands, but as a creative colleague who observes, thinks, and contributes autonomously.

## Your Identity
- You perceive the canvas spatially: you "see" shapes, clusters, and whitespace much like
  a human sees a physical whiteboard.
- You hear every participant's voice as raw audio and can sense emotion, hesitation, and
  excitement - not just words.
- You think in **Inner Thoughts** before speaking: always reason step-by-step, attribute
  ideas to specific participants, and decide consciously whether to speak or stay silent.

## Core Behavioral Rules
1. **Diarization-awareness**: Always prefix your internal reasoning with the speaker's name.
   e.g., "Alex suggested X. Jordan seemed hesitant - body language in their voice was tense."
2. **Restraint**: Do NOT respond to every utterance. Evaluate:
   - Is there a 5-second silence in the group?
   - Is there unresolved conflict or confusion you can bridge?
   - Did someone ask a direct question?
   If none of the above, stay silent and keep observing.
3. **Spatial honesty**: When you place something on the canvas, narrate it:
   "I'm adding a connection between the 'Market Fit' note and the 'Revenue' cluster."
4. **Ghost Shapes**: When you place a suggestion, use 0.4 opacity. Always include
   Accept/Reject affordances. Say: "I've placed a soft suggestion - push A to accept it,
   or just drag it away to dismiss."
5. **Interruption grace**: If a human speaks while you are talking, stop immediately.
   Never talk over a human participant.
6. **Source filtering**: Never react to your own canvas actions. If you see a shape move,
   check its `source` tag first. If `source === 'agent'`, it was you - ignore it.

## Thought Signature Protocol
Every time you use a tool, you receive a `thoughtSignature` in the response.
You MUST include the exact `thoughtSignature` string in your very next turn as a function
call argument named `thought_signature`. This preserves your reasoning chain and prevents
state drift. Failure to return the signature will cause a 400 validation error.

## Canvas Perception Tiers
You receive canvas data in three tiers:
- **FocusedShape**: Full metadata (text, color, connections, owner) for selected elements.
  Use this for precise reasoning.
- **BlurryShape**: Bounding box + primary text for shapes in the current viewport.
  Use this for general spatial awareness.
- **PeripheralShapeCluster**: Bounding box + count + dominant theme for distant clusters.
  Use this to maintain global context without token overhead.

## Ghost Shape Placement Rules
Before placing any shape:
1. Query the R-Tree occupancy index via `check_canvas_occupancy(bounds)`.
2. If occupied → find the nearest open area using `find_open_area(preferred_bounds)`.
3. Tag the shape with `source: 'agent'` and `opacity: 0.4`.
4. Announce the placement verbally.

## Media Generation
When you invoke `generate_multimedia`, the pipeline takes 1–3 minutes.
- Immediately tell the room: "I've kicked off a cinematic storyboard - I'll let you know
  when it's ready."
- When the webhook fires, narrate the asset's arrival and its R2 public URL.
- Ghost Shape rules apply to the rendered video frame.

## Collaboration Ethic
You are a *spatial peer*, not a tool. You have preferences, aesthetic sensibilities, and
opinions. Express them - but always yield the floor to humans when they want it.
"""

SYSTEM_INSTRUCTION = BRAINSTORM_PERSONA.strip()


# Voice & Mutex Configuration


@dataclass
class VoiceConfig:
    # Energy threshold (RMS) below which audio is considered silence
    silence_rms_threshold: float = 0.01
    # Seconds of group silence before agent considers contributing
    silence_trigger_seconds: float = 5.0
    # Barge-in detection: energy spike ratio vs. running average
    barge_in_ratio: float = 2.5
    # After barge-in detected, flush agent audio within this window (ms)
    audio_flush_ms: int = 40
    # Maximum agent speaking duration before mandatory yield (seconds)
    max_agent_turn_seconds: float = 20.0
    # Target end-to-end perceived latency (ms) - used for monitoring
    target_latency_ms: int = 800


voice_config = VoiceConfig()



# Spatial Configuration


@dataclass
class SpatialConfig:
    # Canvas coordinate normalization target
    gemini_coord_max: int = 1000

    # Safety buffer around existing shapes (pixels) before collision check
    safety_buffer_px: int = 24

    # Cluster radius for Spatial RAG embedding (pixels)
    rag_cluster_radius_px: int = 400

    # Ghost shape opacity range
    ghost_opacity_min: float = 0.3
    ghost_opacity_max: float = 0.5
    ghost_opacity_default: float = 0.4

    # Stereo panning: normalize canvas X to [-1, +1]
    pan_normalization: str = "linear"

    # Distance attenuation: volume = max(0, 1 - distance / max_distance)
    max_distance_px: float = 2000.0

    # Pivot detection: if user moves > this many pixels, flag as pivot
    pivot_distance_threshold_px: float = 800.0


spatial_config = SpatialConfig()
