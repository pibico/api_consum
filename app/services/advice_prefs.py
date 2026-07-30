"""Advice email opt-in + send ledger (S7/S8, migration 014).

Minimal consent model: one row per household (`consum.advice_prefs`),
recipient = the api_auth user's email CAPTURED at toggle time (no live
org-membership lookup needed at send time — mirrors `consum.billing_prefs`).
`consum.sent_advice` is the cooldown/daily-cap dedupe ledger the scheduler
consults before every send.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from app.core import db


async def get(customer_id: str) -> Optional[Dict[str, Any]]:
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                "SELECT customer_id, opt_in, recipient_email, lang, region "
                "FROM consum.advice_prefs WHERE customer_id = %s", (customer_id,))
            r = await cur.fetchone()
    if not r:
        return None
    return {"customer_id": str(r[0]), "opt_in": r[1], "recipient_email": r[2],
           "lang": r[3], "region": r[4]}


async def set_opt_in(customer_id: str, opt_in: bool, recipient_email: Optional[str],
                     lang: str = "es", region: Optional[str] = None,
                     updated_by: Optional[str] = None) -> Dict[str, Any]:
    """Upsert the household's preference. Called from the settings-panel
    toggle (S8) — `recipient_email` is always `ctx.user.email` at call time."""
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                """INSERT INTO consum.advice_prefs
                     (customer_id, opt_in, recipient_email, lang, region, updated_by, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, now())
                   ON CONFLICT (customer_id) DO UPDATE SET
                     opt_in = EXCLUDED.opt_in,
                     recipient_email = COALESCE(EXCLUDED.recipient_email, consum.advice_prefs.recipient_email),
                     lang = EXCLUDED.lang,
                     region = COALESCE(EXCLUDED.region, consum.advice_prefs.region),
                     updated_by = EXCLUDED.updated_by,
                     updated_at = now()""",
                (customer_id, opt_in, recipient_email, lang, region, updated_by),
            )
        await con.commit()
    return await get(customer_id)


async def opted_in_households() -> List[Dict[str, Any]]:
    """Every household with opt_in=true — the scheduler's fleet fan-out list.
    Joined with `customers` for the slug (all downstream oe3/exo readers key
    off slugs, not raw UUIDs)."""
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                """SELECT p.customer_id, c.slug, p.recipient_email, p.lang, p.region
                     FROM consum.advice_prefs p
                     JOIN public.customers c ON c.customer_id = p.customer_id
                    WHERE p.opt_in AND p.recipient_email IS NOT NULL""")
            rows = await cur.fetchall()
    return [{"customer_id": str(r[0]), "slug": r[1], "recipient_email": r[2],
            "lang": r[3] or "es", "region": r[4]} for r in rows]


async def last_sent_at(customer_id: str) -> Optional[datetime]:
    """Most recent successful send (any cadence) — backs the cooldown gate."""
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                "SELECT MAX(sent_at) FROM consum.sent_advice "
                "WHERE customer_id = %s AND ok", (customer_id,))
            r = await cur.fetchone()
    return r[0] if r and r[0] else None


async def already_sent_today(customer_id: str, cadence: str,
                             on_date: Optional[date] = None) -> bool:
    """Daily-cap check for THIS cadence — the (customer_id, cadence,
    sent_date) unique constraint is the actual cap; this is the pre-check so
    the scheduler can skip cheaply instead of relying on a DB error."""
    d = on_date or date.today()
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM consum.sent_advice "
                "WHERE customer_id = %s AND cadence = %s AND sent_date = %s AND ok",
                (customer_id, cadence, d))
            return (await cur.fetchone()) is not None


async def mark_sent(customer_id: str, cadence: str, ok: bool = True,
                    on_date: Optional[date] = None) -> None:
    """Record the send attempt. ON CONFLICT DO UPDATE so a retried FAILED
    attempt on the same day can flip to ok=true without a duplicate row."""
    d = on_date or date.today()
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                """INSERT INTO consum.sent_advice (customer_id, cadence, sent_date, ok)
                   VALUES (%s, %s, %s, %s)
                   ON CONFLICT (customer_id, cadence, sent_date)
                   DO UPDATE SET ok = EXCLUDED.ok, sent_at = now()""",
                (customer_id, cadence, d, ok),
            )
        await con.commit()


async def anomaly_already_notified(customer_id: str, event_id: int) -> bool:
    """Hard per-EVENT idempotency (migration 016, consum.notified_anomaly_events)
    — the durable "have we EVER emailed THIS exact anomaly_event id" check.
    Unlike `already_sent_today`, this is NOT keyed by calendar day: the same
    event keeps reappearing in api_edge's `/anomalies` lookback window
    (ANOMALY_LOOKBACK_DAYS) across many 15-min poll cycles, so a per-day
    check alone would let it re-fire on the next calendar day. Only rows
    with ok=true count as "notified" — a failed/dark-launch attempt (ok=false)
    is retried on the next poll, mirroring `already_sent_today`'s `AND ok`."""
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                "SELECT ok FROM consum.notified_anomaly_events "
                "WHERE customer_id = %s AND anomaly_event_id = %s",
                (customer_id, event_id))
            r = await cur.fetchone()
    return bool(r and r[0])


async def mark_anomaly_notified(customer_id: str, event_id: int, ok: bool = True) -> None:
    """Record the anomaly-email attempt for this exact event (migration 016).
    ON CONFLICT DO UPDATE so a retried FAILED attempt can flip to ok=true
    without a duplicate row — same pattern as `mark_sent`."""
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                """INSERT INTO consum.notified_anomaly_events (customer_id, anomaly_event_id, ok)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (customer_id, anomaly_event_id)
                   DO UPDATE SET ok = EXCLUDED.ok, notified_at = now()""",
                (customer_id, event_id, ok),
            )
        await con.commit()


def cooldown_ok(last_sent: Optional[datetime], cooldown_hours: int) -> bool:
    """True when enough time has passed since the last send (any cadence)."""
    if last_sent is None:
        return True
    now = datetime.now(timezone.utc)
    if last_sent.tzinfo is None:
        last_sent = last_sent.replace(tzinfo=timezone.utc)
    return (now - last_sent) >= timedelta(hours=cooldown_hours)
