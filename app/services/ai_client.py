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


def _extract_tool_calls(data: Any) -> List[Dict[str, Any]]:
    """Provider-agnostic tool-call extraction. Known shapes:
    - ollama: message.tool_calls[].function {name, arguments} (gpt-oss emits
      these from a prompt-level tool spec, with content='' and the reasoning
      in message.thinking)
    - openai-style: choices[0].message.tool_calls[].function
    Names may arrive prefixed ('tool_list_sensors') — callers normalize."""
    if not isinstance(data, dict):
        return []
    msg = data.get("message") or {}
    if not isinstance(msg, dict):
        msg = {}
    calls = msg.get("tool_calls")
    if not calls:
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            calls = (choices[0].get("message") or {}).get("tool_calls")
    out = []
    for c in calls or []:
        fn = (c or {}).get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                import json as _json
                args = _json.loads(args)
            except ValueError:
                args = {}
        out.append({"name": str(name), "args": args if isinstance(args, dict) else {},
                    "id": c.get("id")})
    return out


async def llm_chat_full(messages: List[Dict[str, Any]],
                        temperature: float = 0.4,
                        max_tokens: int = 1024,
                        tools: Optional[List[Dict[str, Any]]] = None
                        ) -> Optional[Dict[str, Any]]:
    """One chat turn → {"text", "tool_calls" (normalized), "raw_tool_calls"
    (provider shape, replayable in an assistant message)}. None only on
    transport/parse failure. `tools` = native function schemas (the gateway
    forwards them verbatim; verified working with ollama/gpt-oss)."""
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
    if tools:
        body["tools"] = tools
    try:
        client = http_client.get_client()
        r = await client.post(url, json=body,
                              headers={"X-API-Key": settings.CHAT_API_KEY},
                              timeout=90.0)
        if r.status_code != 200:
            logger.warning("AIDA /llm/chat -> %s: %s", r.status_code, r.text[:200])
            return None
        data = r.json()
        msg = data.get("message") if isinstance(data, dict) else {}
        raw_calls = (msg or {}).get("tool_calls") or []
        # Token accounting (billing ledger): ollama exposes *_eval_count;
        # openai-style providers expose usage.{prompt,completion}_tokens.
        usage = data.get("usage") or {}
        prompt_t = int(data.get("prompt_eval_count")
                       or usage.get("prompt_tokens") or 0)
        completion_t = int(data.get("eval_count")
                           or usage.get("completion_tokens") or 0)
        return {"text": _extract_text(data),
                "tool_calls": _extract_tool_calls(data),
                "raw_tool_calls": raw_calls,
                "usage": {"prompt_tokens": prompt_t,
                          "completion_tokens": completion_t}}
    except httpx.RequestError as e:
        logger.error("AIDA unreachable: %s", e)
        return None
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        logger.warning("AIDA response parse failed: %s", e)
        return None


async def llm_chat(messages: List[Dict[str, str]],
                   temperature: float = 0.4,
                   max_tokens: int = 1024) -> Optional[str]:
    """messages = [{role, content}, …] → assistant text (None on any failure —
    callers degrade gracefully, the page never breaks on AI)."""
    got = await llm_chat_full(messages, temperature=temperature,
                              max_tokens=max_tokens)
    if not got:
        return None
    if not got.get("text") and not got.get("tool_calls"):
        logger.warning("AIDA response had no text/tool_calls")
    return got.get("text") or None
