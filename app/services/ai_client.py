"""AIDA (LLM Chat Service, api.pibico.es/chat) client — F4.

Server-side only: the browser NEVER talks to AIDA directly and the key never
leaves this process. Uses the stateless `POST /api/v1/llm/chat` (normalized
response, no remote conversation state) so every call carries exactly the
AGGREGATED context we choose — raw sensor rows or PII are never sent.

AI is OFF until both CHAT_API_KEY and CHAT_MODEL are set in .env.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import httpx

from app.core import http_client
from app.core.config import settings

logger = logging.getLogger("consum.ai")


def configured() -> bool:
    return bool(settings.CHAT_API_KEY and settings.CHAT_MODEL)


def _extract_text(data: Any) -> Optional[str]:
    """Defensive parse — /llm/chat is 'normalized' but untyped in its spec."""
    if isinstance(data, str):
        return data
    if not isinstance(data, dict):
        return None
    for key in ("content", "response", "text", "answer"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v
    msg = data.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("content"), str):
        return msg["content"]
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
        m = choices[0].get("message") or {}
        if isinstance(m.get("content"), str):
            return m["content"]
    return None


async def llm_chat(messages: List[Dict[str, str]],
                   temperature: float = 0.4,
                   max_tokens: int = 1024) -> Optional[str]:
    """messages = [{role, content}, …] → assistant text (None on any failure —
    callers degrade gracefully, the page never breaks on AI)."""
    if not configured():
        return None
    url = f"{settings.CHAT_BASE_URL.rstrip('/')}/api/v1/llm/chat"
    body = {
        "provider": settings.CHAT_PROVIDER,
        "model": settings.CHAT_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    try:
        client = http_client.get_client()
        r = await client.post(url, json=body,
                              headers={"X-API-Key": settings.CHAT_API_KEY},
                              timeout=90.0)
        if r.status_code != 200:
            logger.warning("AIDA /llm/chat -> %s: %s", r.status_code, r.text[:200])
            return None
        text = _extract_text(r.json())
        if not text:
            logger.warning("AIDA response shape not recognized: %s", str(r.json())[:200])
        return text
    except httpx.RequestError as e:
        logger.error("AIDA unreachable: %s", e)
        return None
