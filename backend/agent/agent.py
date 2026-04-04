"""
agent/agent.py — LiveKit entry point for the AI Brainstorm Canvas agent.

Responsibilities:
  - Connect to a LiveKit room as a native peer participant.
  - Use Gemini 3.1 Flash Live (A2A) via livekit.plugins.google.beta.realtime.RealtimeModel.
  - Implement voice mutex with interruptibility (barge-in detection).
  - Maintain speaker diarization and Inner Thoughts attribution map.
  - Subscribe to canvas state via Redis pub/sub; apply spatial panning/volume metadata.
  - Run a decision engine that controls WHEN the agent speaks.
  - Support two editorial modes:
      "autonomous"  — act directly on the canvas without asking.
      "permission"  — publish a permission_request event and await human approval.
  - Register all Gemini function-calling tools.
  - Run the background perception loop (canvas state -> tiered perception -> Vectorize).
  - Manage context window compression and periodic snapshot lifecycle.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict, deque
from typing import Any, Optional

import boto3
import numpy as np
import redis.asyncio as aioredis
from dotenv import load_dotenv

from livekit import agents, rtc
from livekit.agents import AgentSession, JobContext, WorkerOptions, cli
from livekit.plugins.google.beta.realtime import RealtimeModel

from agent.config import (
    SYSTEM_INSTRUCTION,
    caching_config,
    env,
    spatial_config,
    voice_config,
)
from agent.perception_loop import PerceptionLoop
from agent.spatial_utils import (
    CanvasBounds,
    PivotDetector,
    PromptPartUtil,
    RTreeIndex,
    SnapshotManager,
    SpatialRAG,
    canvas_to_stereo_pan,
    canvas_to_volume,
    perception_tier_to_prompt,
)
from agent.tools import (
    TOOL_DECLARATIONS,
    TOOL_HANDLERS,
    init_tools,
)

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("brainstorm.agent")



class InnerThoughts:
    """
    Diarized record of utterances attributed to specific participants.
    The agent's reasoning references this map before contributing:
      "Alex suggested X. Jordan appears hesitant."
    """

    def __init__(self) -> None:
        self._log: dict[str, deque] = defaultdict(lambda: deque(maxlen=50))

    def record(self, identity: str, text: str, emotion_hint: str = "") -> None:
        self._log[identity].append({
            "ts": time.time(),
            "text": text,
            "emotion": emotion_hint,
        })

    def get_context_string(self) -> str:
        """Compact representation for injection into Gemini context."""
        parts: list[str] = []
        for identity, entries in self._log.items():
            recent = list(entries)[-5:]
            utterances = " | ".join(
                f"[{e['emotion'] or 'neutral'}] {e['text']}"
                for e in recent
            )
            parts.append(f"{identity}: {utterances}")
        return "\n".join(parts) if parts else "(no conversation yet)"

    def get_participants(self) -> list[str]:
        return list(self._log.keys())



class VoiceMutex:
    """
    Controls the agent's speaking floor.
    Barge-in detection immediately sets interrupt_event, which the
    RealtimeModel monitors to flush its outgoing audio buffer.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.interrupt_event = asyncio.Event()
        self._agent_speaking = False
        self._speaking_start: float = 0.0

    async def acquire_floor(self) -> bool:
        if self._lock.locked():
            return False
        await self._lock.acquire()
        self._agent_speaking = True
        self._speaking_start = time.time()
        self.interrupt_event.clear()
        return True

    def release_floor(self) -> None:
        self._agent_speaking = False
        if self._lock.locked():
            self._lock.release()

    def trigger_barge_in(self) -> None:
        logger.info("Barge-in detected — releasing agent floor")
        self.interrupt_event.set()
        self.release_floor()

    @property
    def is_agent_speaking(self) -> bool:
        return self._agent_speaking

    @property
    def speaking_duration(self) -> float:
        if not self._agent_speaking:
            return 0.0
        return time.time() - self._speaking_start



class DecisionEngine:
    """
    Evaluates whether the agent should contribute to the conversation.

    SPEAK triggers:
      - Direct keyword invocation ("Muse", "hey AI", "what do you think")
      - Open question addressed to the group
      - Group silence exceeds silence_trigger_seconds
      - New canvas cluster detected

    SILENT suppressions:
      - Human is currently speaking
      - Agent spoke within the last 10 seconds
      - Canvas pivot is active (users moved to a new area)
    """

    TRIGGER_KEYWORDS = frozenset({
        "muse", "hey ai", "what do you think", "any ideas", "help",
        "suggestions", "what about", "agent", "brainstorm",
    })

    def __init__(self) -> None:
        self._last_spoke_at: float = 0.0
        self._last_human_spoke_at: float = time.time()
        self._human_speaking: set[str] = set()

    def on_human_speak_start(self, identity: str) -> None:
        self._human_speaking.add(identity)
        self._last_human_spoke_at = time.time()

    def on_human_speak_end(self, identity: str) -> None:
        self._human_speaking.discard(identity)
        self._last_human_spoke_at = time.time()

    def on_agent_spoke(self) -> None:
        self._last_spoke_at = time.time()

    def should_speak(
        self,
        latest_transcript: str = "",
        canvas_event: Optional[str] = None,
        pivot_active: bool = False,
    ) -> tuple[bool, str]:
        """Returns (should_speak, reason)."""
        now = time.time()

        if pivot_active:
            return False, "pivot active — observing"
        if self._human_speaking:
            return False, "human speaking"
        if now - self._last_spoke_at < 10.0:
            return False, "agent cooldown"

        lower = latest_transcript.lower()
        if any(kw in lower for kw in self.TRIGGER_KEYWORDS):
            return True, "direct invocation"

        if lower.strip().endswith("?") and len(lower.split()) > 3:
            return True, "open question detected"

        silence_duration = now - self._last_human_spoke_at
        if silence_duration >= voice_config.silence_trigger_seconds:
            return True, f"group silence ({silence_duration:.1f}s)"

        if canvas_event == "new_cluster":
            return True, "new canvas cluster detected"

        return False, "no trigger"



class PresenceMap:
    """Tracks participant cursor positions for spatial audio computation."""

    def __init__(self) -> None:
        self._positions: dict[str, tuple[float, float]] = {}

    def update(self, identity: str, x: float, y: float) -> None:
        self._positions[identity] = (x, y)

    def get(self, identity: str) -> Optional[tuple[float, float]]:
        return self._positions.get(identity)

    def get_pan_and_volume(
        self,
        speaker_id: str,
        listener_id: str,
        canvas_w: float,
        canvas_h: float,
    ) -> tuple[float, float]:
        """Returns (pan[-1,1], volume[0,1]) for Web Audio API PannerNode."""
        speaker_pos = self._positions.get(speaker_id)
        listener_pos = self._positions.get(listener_id)
        if not speaker_pos or not listener_pos:
            return 0.0, 1.0
        pan = canvas_to_stereo_pan(speaker_pos[0], listener_pos[0], canvas_w)
        vol = canvas_to_volume(speaker_pos, listener_pos)
        return pan, vol



class CanvasEventSubscriber:
    """
    Subscribes to canvas:events:{board_id} on Redis.
    Updates RTreeIndex, PresenceMap, PivotDetector, and SnapshotManager.
    Events with source='agent' are filtered to prevent feedback loops.
    Also watches for approval events and writes them to Redis for tool handlers.
    """

    def __init__(
        self,
        board_id: str,
        redis_client: aioredis.Redis,
        rtree: RTreeIndex,
        presence: PresenceMap,
        pivot: PivotDetector,
        snapshot_mgr: SnapshotManager,
        canvas_w: float,
        canvas_h: float,
    ) -> None:
        self.board_id = board_id
        self.redis = redis_client
        self.rtree = rtree
        self.presence = presence
        self.pivot = pivot
        self.snapshot = snapshot_mgr
        self.canvas_w = canvas_w
        self.canvas_h = canvas_h
        self._task: Optional[asyncio.Task] = None
        self._last_canvas_event: Optional[str] = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._listen())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    @property
    def last_event(self) -> Optional[str]:
        return self._last_canvas_event

    async def _listen(self) -> None:
        channel = f"canvas:events:{self.board_id}"
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(channel)
        logger.info("Canvas subscriber active on %s", channel)

        async for message in pubsub.listen():
            if message["type"] != "message":
                continue
            try:
                event = json.loads(message["data"])
                await self._handle_event(event)
            except Exception as exc:
                logger.warning("Canvas event parse error: %s", exc)

    async def _handle_event(self, event: dict) -> None:
        event_type = event.get("type")
        source = (
            event.get("source")
            or event.get("shape", {}).get("meta", {}).get("source")
        )

        # Feedback mitigation: discard events originating from this agent.
        if source == "agent":
            logger.debug("Filtered agent-sourced event: %s", event_type)
            return

        if event_type == "shape_added":
            shape = event.get("shape", {})
            shape_id = shape.get("id")
            props = shape.get("props", {})
            bounds = CanvasBounds(
                x=float(shape.get("x", 0)),
                y=float(shape.get("y", 0)),
                w=float(props.get("w", 200)),
                h=float(props.get("h", 100)),
            )
            if shape_id:
                await self.rtree.insert(shape_id, bounds)
            self._last_canvas_event = "shape_added"

        elif event_type == "shape_removed":
            shape = event.get("shape", {})
            shape_id = shape.get("id")
            props = shape.get("props", {})
            bounds = CanvasBounds(
                x=float(shape.get("x", 0)),
                y=float(shape.get("y", 0)),
                w=float(props.get("w", 200)),
                h=float(props.get("h", 100)),
            )
            if shape_id:
                await self.rtree.delete(shape_id, bounds)
            self._last_canvas_event = "shape_removed"

        elif event_type == "cursor_move":
            identity = event.get("identity")
            x = float(event.get("x", 0))
            y = float(event.get("y", 0))
            if identity:
                self.presence.update(identity, x, y)
                self.pivot.update_position(identity, x, y)

                # Publish pivot event to Redis so active workers can cancel.
                if self.pivot.pivot_event.is_set():
                    await self.redis.set(
                        f"pivot:{self.board_id}", "1", ex=30
                    )

        elif event_type == "board_state":
            state = event.get("state")
            if state:
                self.snapshot.update_state(state)

        elif event_type == "new_cluster":
            self._last_canvas_event = "new_cluster"

        elif event_type == "permission_response":
            # Human clicked Accept or Reject on a permission request card.
            request_id = event.get("request_id")
            decision = event.get("decision")  # "approved" | "rejected"
            if request_id and decision in ("approved", "rejected"):
                await self.redis.set(
                    f"approval:{self.board_id}:{request_id}",
                    decision,
                    ex=120,
                )
                logger.info(
                    "Permission %s for request %s on board %s",
                    decision, request_id, self.board_id,
                )

        elif event_type == "snapshot_request":
            await self.redis.set(f"snapshot_request:{self.board_id}", "1", ex=60)

        elif event_type == "asset_ready":
            # Higgsfield asset arrived via webhook — the agent should narrate.
            self._last_canvas_event = "asset_ready"



class SilenceMonitor:
    """
    Tracks per-participant RMS audio level.
    Detects group silence and barge-in events.
    """

    def __init__(self, mutex: VoiceMutex, decision: DecisionEngine) -> None:
        self.mutex = mutex
        self.decision = decision
        self._rms_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=20))

    def process_audio_frame(self, identity: str, pcm_samples: np.ndarray) -> None:
        rms = float(np.sqrt(np.mean(pcm_samples.astype(np.float32) ** 2)))
        history = self._rms_history[identity]
        history.append(rms)

        is_speaking = rms > voice_config.silence_rms_threshold
        if is_speaking:
            self.decision.on_human_speak_start(identity)
            if self.mutex.is_agent_speaking:
                avg_rms = np.mean(list(history)[:-1]) if len(history) > 1 else rms
                if avg_rms > 0 and (rms / avg_rms) >= voice_config.barge_in_ratio:
                    self.mutex.trigger_barge_in()
        else:
            self.decision.on_human_speak_end(identity)



def _infer_emotion_hint(text: str) -> str:
    """Lightweight keyword-based emotion classification."""
    lower = text.lower()
    if any(w in lower for w in ["love", "great", "amazing", "yes!", "exactly", "perfect"]):
        return "enthusiastic"
    if any(w in lower for w in ["but", "wait", "no", "disagree", "not sure", "hmm"]):
        return "hesitant"
    if any(w in lower for w in ["?", "how", "why", "what if", "could we"]):
        return "curious"
    if any(w in lower for w in ["stuck", "confused", "don't understand", "lost"]):
        return "confused"
    return "neutral"



async def entrypoint(ctx: JobContext) -> None:
    """
    LiveKit agent entrypoint. Called once per room when a participant joins.

    Agent mode is read from Redis key  agent_mode:{room_name}.
    The FastAPI PATCH /boards/{id}/mode endpoint writes this key.
    Defaults to 'permission' if not set.
    """
    logger.info("Agent starting for room: %s", ctx.room.name)
    await ctx.connect()

    redis_client = aioredis.from_url(env.redis_url, decode_responses=True)
    r2_client = boto3.client(
        "s3",
        endpoint_url=env.r2_endpoint,
        aws_access_key_id=env.r2_access_key,
        aws_secret_access_key=env.r2_secret_key,
        region_name="auto",
    )

    board_id = ctx.room.name
    canvas_w = 4096.0
    canvas_h = 4096.0

    rtree = RTreeIndex()
    spatial_rag = SpatialRAG()
    snapshot_mgr = SnapshotManager(board_id=board_id, r2_client=r2_client)

    perception_loop = PerceptionLoop(
        board_id=board_id,
        redis_client=redis_client,
        rag=spatial_rag,
        snapshot_mgr=snapshot_mgr,
        canvas_w=canvas_w,
        canvas_h=canvas_h,
    )

    init_tools(
        rtree=rtree,
        rag=spatial_rag,
        snapshot_mgr=snapshot_mgr,
        redis_client=redis_client,
        room_name=board_id,
        perception_loop=perception_loop,
    )

    inner_thoughts = InnerThoughts()
    mutex = VoiceMutex()
    decision_engine = DecisionEngine()
    presence = PresenceMap()
    pivot_detector = PivotDetector()
    silence_monitor = SilenceMonitor(mutex, decision_engine)

    canvas_sub = CanvasEventSubscriber(
        board_id=board_id,
        redis_client=redis_client,
        rtree=rtree,
        presence=presence,
        pivot=pivot_detector,
        snapshot_mgr=snapshot_mgr,
        canvas_w=canvas_w,
        canvas_h=canvas_h,
    )

    await canvas_sub.start()
    await perception_loop.start()
    await snapshot_mgr.start(interval=caching_config.snapshot_interval_seconds)

    initial_mode = await redis_client.get(f"agent_mode:{board_id}") or "permission"
    await redis_client.set(f"agent_mode:{board_id}", initial_mode)
    logger.info("Agent mode for room %s: %s", board_id, initial_mode)

    model = RealtimeModel(
        model=env.gemini_model,
        api_key=env.gemini_api_key,
        instructions=SYSTEM_INSTRUCTION,
        tools=TOOL_DECLARATIONS,
        voice="Charon",
        temperature=0.75,
        context_window_compression={
            "trigger_tokens": caching_config.compression_trigger_tokens,
            "target_tokens": caching_config.compression_target_tokens,
        },
    )

    session = AgentSession(llm=model)

    @session.on("function_calls_collected")
    async def on_function_calls(
        calls: list[agents.llm.FunctionCall],
    ) -> None:
        """
        Route collected function calls to their handlers.
        The thought_signature from each call's args is echoed in the response
        so Gemini never loses its internal reasoning chain.
        """
        for call in calls:
            tool_name = call.function_name
            try:
                tool_args = json.loads(call.arguments) if isinstance(call.arguments, str) else call.arguments
            except Exception:
                tool_args = {}

            handler = TOOL_HANDLERS.get(tool_name)
            if not handler:
                logger.warning("Unknown tool: %s", tool_name)
                result = {"error": f"Unknown tool: {tool_name}"}
            else:
                logger.info(
                    "Tool call: %s args=%s",
                    tool_name,
                    {k: v for k, v in tool_args.items() if k != "thought_signature"},
                )
                try:
                    result = await handler(**tool_args)
                except Exception as exc:
                    logger.error("Tool %s failed: %s", tool_name, exc, exc_info=True)
                    result = {
                        "error": str(exc),
                        "thought_signature": tool_args.get("thought_signature"),
                    }

            await session.submit_tool_result(call_id=call.call_id, result=result)

    @ctx.room.on("track_subscribed")
    def on_track_subscribed(
        track: rtc.Track,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if track.kind != rtc.TrackKind.KIND_AUDIO:
            return

        identity = participant.identity
        try:
            metadata = json.loads(participant.metadata or "{}")
        except Exception:
            metadata = {}

        display_name = metadata.get("display_name", identity)
        logger.info("Audio track subscribed: %s (%s)", identity, display_name)
        inner_thoughts.record(identity, f"[joined as '{display_name}']")

        asyncio.ensure_future(_process_audio_track(track, identity))

    async def _process_audio_track(track: rtc.Track, identity: str) -> None:
        audio_stream = rtc.AudioStream(track)
        async for frame_event in audio_stream:
            frame = frame_event.frame
            samples = np.frombuffer(frame.data, dtype=np.int16)
            silence_monitor.process_audio_frame(identity, samples)
        decision_engine.on_human_speak_end(identity)
        logger.info("Audio stream ended for %s", identity)

    @session.on("user_speech_committed")
    async def on_user_speech_committed(msg: agents.llm.ChatMessage) -> None:
        """
        Called after a participant's speech turn is committed.

        CRITICAL — RealtimeModel (A2A) protocol:
          The Gemini RealtimeModel auto-generates an audio response the moment
          a user speech turn is committed.  We must not call session.say() with
          raw context here — that creates a second, empty conflicting turn which
          triggers the 'model output must contain either output text or tool calls'
          400 error.

          Correct pattern:
            - If DecisionEngine says SILENT  → interrupt() the auto-response.
            - If DecisionEngine says SPEAK   → do nothing; track the floor so
              the VoiceMutex barge-in logic can interrupt on demand.
            - Context is kept current by _context_updater() which rewrites the
              system instruction every 5 s via model.update_options().
        """
        identity = getattr(msg, "participant_identity", None) or msg.role or "unknown"
        text = msg.content or ""

        inner_thoughts.record(
            identity=identity,
            text=text,
            emotion_hint=_infer_emotion_hint(text),
        )

        pivot_active = pivot_detector.pivot_event.is_set()

        should_speak, reason = decision_engine.should_speak(
            latest_transcript=text,
            canvas_event=canvas_sub.last_event,
            pivot_active=pivot_active,
        )

        logger.info(
            "Decision: should_speak=%s reason='%s' pivot=%s",
            should_speak, reason, pivot_active,
        )

        if pivot_active:
            pivot_detector.clear_pivot()
            # Signal active Higgsfield workers to cancel.
            await redis_client.set(f"pivot:{board_id}", "1", ex=30)

        if not should_speak:
            # Suppress the model's auto-response for this turn.
            try:
                await session.interrupt()
            except Exception:
                pass  # interrupt is best-effort if no response is in progress
            return

        # The model is already generating an audio response — we track the floor
        # so barge-in detection can interrupt it if a human speaks.
        floor_acquired = await mutex.acquire_floor()
        if not floor_acquired:
            logger.info("Floor busy — skipping floor tracking")
            return

        try:
            decision_engine.on_agent_spoke()
            # Monitor for barge-in or max turn duration.
            end_time = time.time() + voice_config.max_agent_turn_seconds
            while time.time() < end_time:
                if mutex.interrupt_event.is_set():
                    logger.info("Agent yielding floor — barge-in")
                    try:
                        await session.interrupt()
                    except Exception:
                        pass
                    break
                await asyncio.sleep(0.05)
        finally:
            mutex.release_floor()

    async def _context_updater() -> None:
        """
        Periodically rewrites the model's system instruction with:
          - Latest tiered canvas perception summary (from Redis).
          - Recent speaker attribution from InnerThoughts.
          - Current agent mode.
        This keeps Gemini's spatial awareness current without consuming
        a full content turn (which would conflict with RealtimeModel A2A).
        """
        while True:
            await asyncio.sleep(5.0)
            try:
                perception_summary = await perception_loop.get_latest_summary()
                thoughts_summary = inner_thoughts.get_context_string()
                mode = await redis_client.get(f"agent_mode:{board_id}") or "permission"
                updated_instruction = (
                    f"{SYSTEM_INSTRUCTION}\n\n"
                    f"## Live Canvas State\n{perception_summary}\n\n"
                    f"## Recent Conversation\n{thoughts_summary}\n\n"
                    f"## Agent Mode\n{mode}"
                )
                model.update_options(instructions=updated_instruction)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Context updater error (non-fatal): %s", exc)

    context_updater_task = asyncio.create_task(_context_updater())

    await session.start(ctx.room)
    logger.info("Agent session active in room: %s", board_id)

    try:
        await asyncio.Event().wait()
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        context_updater_task.cancel()
        try:
            await context_updater_task
        except asyncio.CancelledError:
            pass
        await canvas_sub.stop()
        await perception_loop.stop()
        await snapshot_mgr.stop()
        await redis_client.aclose()
        logger.info("Agent shutdown complete for room: %s", board_id)



if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            api_key=env.livekit_api_key,
            api_secret=env.livekit_api_secret,
            ws_url=env.livekit_url,
        )
    )
