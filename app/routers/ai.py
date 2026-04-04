"""
app/routers/ai.py — AI utility endpoints.

POST /ai/summarize  — Summarize a board's notes and suggest next steps using Gemini.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.core.config import settings
from app.deps import get_current_user
from app.models.user import User

logger = logging.getLogger("ai.router")
router = APIRouter(prefix="/ai", tags=["ai"])


class BoardSummaryRequest(BaseModel):
    board_title: str
    notes: list[str]


class BoardSummaryResponse(BaseModel):
    summary: str
    next_steps: list[str]


@router.post("/summarize", response_model=BoardSummaryResponse)
async def summarize_board(
    data: BoardSummaryRequest,
    current_user: User = Depends(get_current_user),
) -> BoardSummaryResponse:
    """
    Summarize a brainstorm board's sticky notes and return AI-generated next steps.
    Uses Gemini via the google-generativeai SDK.
    Falls back to a structured stub if the API key is not configured or the call fails.
    """
    if not data.notes:
        return BoardSummaryResponse(
            summary=f"Board '{data.board_title}' has no notes yet.",
            next_steps=["Add your first idea to the canvas."],
        )

    if not settings.gemini_api_key:
        logger.warning("GEMINI_API_KEY not set — returning stub summary")
        return _stub_summary(data)

    try:
        import google.generativeai as genai

        genai.configure(api_key=settings.gemini_api_key)
        model = genai.GenerativeModel(model_name=settings.gemini_model)

        notes_block = "\n".join(f"- {note}" for note in data.notes)
        prompt = f"""You are a creative facilitator summarizing a collaborative brainstorming session.

Board title: {data.board_title}

Notes from the session:
{notes_block}

Please provide:
1. A concise summary (2-3 sentences) capturing the key themes and ideas.
2. A list of 3-5 concrete, actionable next steps to move the project forward.

Respond in this exact JSON format:
{{
  "summary": "...",
  "next_steps": ["...", "...", "..."]
}}"""

        response = await model.generate_content_async(
            prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.7,
                response_mime_type="application/json",
            ),
        )

        import json
        result = json.loads(response.text)
        return BoardSummaryResponse(
            summary=result.get("summary", ""),
            next_steps=result.get("next_steps", []),
        )

    except Exception as exc:
        logger.error("Gemini summarize failed: %s", exc, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"AI summarization failed: {exc}",
        )


def _stub_summary(data: BoardSummaryRequest) -> BoardSummaryResponse:
    """Deterministic fallback when Gemini is unavailable."""
    summary = (
        f"Board '{data.board_title}' contains {len(data.notes)} idea(s). "
        "The team has captured a diverse set of thoughts that can be organized into themes."
    )
    next_steps = [
        "Group related notes into clusters on the canvas.",
        "Vote on the top 3 ideas using the annotation tools.",
        "Assign an owner to each shortlisted idea.",
        "Schedule a follow-up session to prototype the winning concept.",
    ]
    return BoardSummaryResponse(summary=summary, next_steps=next_steps)