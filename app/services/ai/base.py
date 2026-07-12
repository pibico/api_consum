"""Skill base — the reusable core every domain skill builds on.

A Skill packages a system prompt + helpers over the shared `ai_client.llm_chat`:
  · `run_json(prompt, Model)` → a pydantic-validated instance (retries once on a
    bad parse, then None) — for structured extraction.
  · `run_text(prompt)` → free assistant text or None — for plain-language output.

Both degrade to None when the AI is off / unreachable / misbehaving, so a page
NEVER breaks because of AI. Contracts use this first; invoices reuse it verbatim.
"""
from __future__ import annotations

import logging
import re
from typing import Optional, Type, TypeVar

from pydantic import BaseModel, ValidationError

from app.services import ai_client

logger = logging.getLogger("consum.ai.skill")

T = TypeVar("T", bound=BaseModel)

_FENCE_RE = re.compile(r"```(?:json)?", re.IGNORECASE)


def _strip_json(text: str) -> str:
    """Pull the JSON value out of an LLM reply — drops markdown fences and any
    prose around the outermost {...} / [...] block."""
    t = _FENCE_RE.sub("", text).replace("```", "").strip()
    starts = [i for i in (t.find("{"), t.find("[")) if i != -1]
    if starts:
        t = t[min(starts):]
    end = max(t.rfind("}"), t.rfind("]"))
    if end != -1:
        t = t[: end + 1]
    return t.strip()


class Skill:
    """Base domain skill. Subclasses set `name` + `system_prompt` and call
    `run_json` / `run_text` from their action methods."""

    name: str = "skill"
    system_prompt: str = ""

    async def run_json(self, user_prompt: str, output_model: Type[T],
                       system: Optional[str] = None,
                       temperature: float = 0.2,
                       max_tokens: int = 1200) -> Optional[T]:
        """Ask the LLM for JSON matching `output_model`; return a validated
        instance or None (AI off / unreachable / unparseable after one retry)."""
        if not ai_client.configured():
            return None
        messages = [
            {"role": "system", "content": system or self.system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        for attempt in (1, 2):
            text = await ai_client.llm_chat(messages, temperature=temperature,
                                            max_tokens=max_tokens)
            if not text:
                return None
            try:
                return output_model.model_validate_json(_strip_json(text))
            except (ValidationError, ValueError) as e:
                logger.warning("%s: JSON validation failed (attempt %s): %s",
                               self.name, attempt, str(e)[:200])
                if attempt == 1:
                    messages = messages + [
                        {"role": "assistant", "content": text[:2000]},
                        {"role": "user", "content":
                         "Devuelve SOLO el objeto JSON válido que cumpla el esquema "
                         "pedido, sin texto adicional, sin explicaciones y sin ```."},
                    ]
        return None

    async def run_text(self, user_prompt: str, system: Optional[str] = None,
                       temperature: float = 0.4,
                       max_tokens: int = 700) -> Optional[str]:
        """Free-text assistant reply (plain-language output) or None."""
        if not ai_client.configured():
            return None
        messages = [
            {"role": "system", "content": system or self.system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        return await ai_client.llm_chat(messages, temperature=temperature,
                                        max_tokens=max_tokens)
