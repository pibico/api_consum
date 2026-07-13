"""AI usage ledger + persisted day chat (mig 010) — pay-per-use accounting.

record() writes one row per billable interaction (org/household + user);
month_tokens() feeds the CREDIT CUTOFF (AI_MONTHLY_TOKEN_CAP: when a scope's
month total crosses it, /ai/ask answers 429 AI_CREDITS). chat_append/chat_get
persist the day's conversation so the /ai page survives navigation.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from app.core import db
from app.core.config import settings


async def record(customer_slug: str, user_email: Optional[str], kind: str,
                 prompt_tokens: int, completion_tokens: int,
                 tool_hops: int = 0) -> None:
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                "INSERT INTO consum.ai_usage (customer_slug, user_email, kind, "
                "model, prompt_tokens, completion_tokens, tool_hops) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (customer_slug, user_email, kind, settings.CHAT_MODEL,
                 prompt_tokens, completion_tokens, tool_hops))


async def month_tokens(customer_slug: str) -> int:
    """Total tokens (prompt+completion) this calendar month for the scope."""
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                "SELECT COALESCE(SUM(prompt_tokens + completion_tokens), 0) "
                "FROM consum.ai_usage WHERE customer_slug = %s "
                "AND ts >= date_trunc('month', now())", (customer_slug,))
            return int((await cur.fetchone())[0])


async def chat_append(customer_slug: str, user_email: Optional[str],
                      role: str, content: str,
                      meta: Optional[Dict[str, Any]] = None) -> None:
    import json
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                "INSERT INTO consum.ai_chat (customer_slug, user_email, role, content, meta) "
                "VALUES (%s, %s, %s, %s, %s)",
                (customer_slug, user_email, role, content[:8000],
                 json.dumps(meta) if meta else None))


async def chat_get(customer_slug: str, user_email: Optional[str],
                   limit: int = 40) -> List[Dict[str, Any]]:
    """Today's conversation for this household+user (oldest first)."""
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                "SELECT role, content, ts, meta FROM consum.ai_chat "
                "WHERE customer_slug = %s AND user_email IS NOT DISTINCT FROM %s "
                "AND day = CURRENT_DATE ORDER BY id DESC LIMIT %s",
                (customer_slug, user_email, limit))
            rows = await cur.fetchall()
    return [{"role": r[0], "content": r[1], "ts": r[2].isoformat(),
             "meta": r[3]}
            for r in reversed(rows)]
