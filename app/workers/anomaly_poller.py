"""Member anomaly feed poller (Phase 2 E4 UI surfacing, 2026-07-29).

Periodically pulls api_edge's `GET /anomalies?slug=` (statistical anomaly
write-path, mig 024 anomaly_events — see api_edge's endpoints/anomalies.py)
for every onboarded household slug and caches the result in-process for the
member-facing `GET /api/v1/anomalies` read endpoint. READ-ONLY surfacing —
no ack/resolve here (that stays in the api_edge admin console per the
Phase 2 spec; a member never mutates another tenant's write-path).

Auth: reuses the EXISTING api_consum -> api_edge service credential
(EDGE_API_KEY, edge_client.py) — the SAME key already used for /devices and
/topology. api_edge's `org_scope()` dependency (rbac.py:128-135) treats a
valid non-admin service key as unrestricted-read, exactly like the other
edge_client.py calls — no new credential, no api_edge-side change needed.

Storage: in-process cache ONLY (module dict slug -> {fetched_at, items}).
A persisted `consum` table (next migration after 014) was considered, but
api_edge's `anomaly_events` hypertable is ALREADY the durable source of
truth for this data — a local copy would just be a second, staler source.
This cache exists purely to avoid calling api_edge on every dashboard load;
losing it on restart is harmless (the next poll or a cold-cache live fetch
repopulates it). Mirrors notify_sub/advice_scheduler's "log-and-degrade,
never block startup" style.

Loop closure (2026-07-30): `_dispatch_new_anomalies` turns a NEW,
sufficiently severe, opted-in-household anomaly into ONE advice email
(reuses the Phase 1 mailer + AdviceSkill via `app.workers.advice_scheduler.
run_for_anomaly`) — see that module and migration 016 for the two dedupe
layers (event-level ledger + household cooldown/cap). Gated behind
`ANOMALY_EMAIL_ENABLED` AND `EMAIL_ADVICE_ENABLED` (both default False —
dark launch); the READ-ONLY cache/UI-feed behaviour above is unaffected
either way.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.services import advice_prefs, consumption, edge_client

logger = logging.getLogger("consum.anomaly_poller")

_SEVERITY_RANK = {"warning": 1, "critical": 2}

_scheduler = None
_cache: Dict[str, Dict[str, Any]] = {}   # slug -> {"fetched_at": epoch|None, "items": list|None}


def cached(slug: str) -> Dict[str, Any]:
    """Last poll result for `slug` — {"fetched_at": None, "items": None} if
    the poller hasn't reached this slug yet (distinct from "polled, found
    zero" — the endpoint uses this to decide whether a live fallback fetch
    is worth it)."""
    return _cache.get(slug) or {"fetched_at": None, "items": None}


async def fetch_live(slug: str) -> List[dict]:
    """One-off live fetch (cold-cache fallback for the read endpoint, and
    the primitive the scheduled poll loop reuses). Updates the cache on
    success; on failure returns [] but leaves any existing cache entry
    untouched (never regress a slug from 'has data' to 'empty' just because
    one poll failed)."""
    items = await edge_client.anomalies(slug, since_days=settings.ANOMALY_LOOKBACK_DAYS)
    if items is not None:
        _cache[slug] = {"fetched_at": time.time(), "items": items}
        return items
    return (_cache.get(slug) or {}).get("items") or []


def _severity_ok(sev: Optional[str]) -> bool:
    want = _SEVERITY_RANK.get((settings.ANOMALY_EMAIL_MIN_SEVERITY or "critical").lower(), 2)
    have = _SEVERITY_RANK.get((sev or "").lower(), 0)
    return have >= want


async def _dispatch_new_anomalies(slug: str, items: List[dict]) -> None:
    """Turn NEW, unnotified, sufficiently severe anomalies into AT MOST ONE
    advice email per household per poll cycle (Phase 2 E4 loop closure,
    2026-07-30). Gated on BOTH `ANOMALY_EMAIL_ENABLED` and
    `EMAIL_ADVICE_ENABLED` as a fast, DB-free, LLM-free no-op while either is
    False — the loop stays "wired but silent" with zero cost until a human
    deliberately flips both flags (unlike the daily/weekly forecast cadences,
    which narrate every day even dark-launched for review; this poller runs
    every ANOMALY_POLL_INTERVAL_MIN minutes, so narrating on every dark tick
    would burn AI budget for nothing actionable).

    Dedupe: event-level idempotency via `advice_prefs.anomaly_already_notified`
    (migration 016, consum.notified_anomaly_events) — the SAME event stays
    inside api_edge's lookback window across many polls, so this is checked
    BEFORE the household cooldown/cap (also migration 016, cadence='anomaly'
    on consum.sent_advice, reusing `already_sent_today`/`last_sent_at`/
    `cooldown_ok`). Candidates are tried newest-first; the loop stops (not
    just skips) at the first cooldown/cap hit — a blocked household this
    poll will simply be picked up again once the window reopens."""
    if not settings.ANOMALY_EMAIL_ENABLED or not settings.EMAIL_ADVICE_ENABLED or not items:
        return

    candidates = [a for a in items
                 if a.get("id") is not None and not a.get("notified")
                 and not a.get("resolved_at") and _severity_ok(a.get("severity"))]
    if not candidates:
        return

    ids = await consumption._slugs_to_ids([slug])
    customer_id = ids.get(slug)
    if not customer_id:
        return

    prefs = await advice_prefs.get(customer_id)
    if not prefs or not prefs.get("opt_in") or not prefs.get("recipient_email"):
        return

    candidates.sort(key=lambda a: a.get("ts") or "", reverse=True)
    from app.workers import advice_scheduler
    for a in candidates:
        event_id = a["id"]
        try:
            if await advice_prefs.anomaly_already_notified(customer_id, event_id):
                continue
            if await advice_prefs.already_sent_today(customer_id, "anomaly"):
                break  # household daily cap reached — try again on a future poll/day
            last_sent = await advice_prefs.last_sent_at(customer_id)
            if not advice_prefs.cooldown_ok(last_sent, settings.ADVICE_COOLDOWN_H):
                break  # cooldown — try again once it lapses
            result = await advice_scheduler.run_for_anomaly(a, prefs, customer_id, slug)
            logger.info("anomaly_poller: dispatch slug=%s event=%s -> %s", slug, event_id, result)
        except Exception as exc:  # noqa: BLE001 — one bad event must never abort the household
            logger.error("anomaly_poller: dispatch slug=%s event=%s failed: %s", slug, event_id, exc)


async def _poll_once() -> None:
    try:
        slugs = await consumption.all_slugs()
    except Exception as exc:  # noqa: BLE001 — a DB blip must not kill the scheduler
        logger.warning("anomaly_poller: could not list slugs: %s", exc)
        return
    for slug in slugs:
        try:
            items = await fetch_live(slug)
            await _dispatch_new_anomalies(slug, items)
        except Exception as exc:  # noqa: BLE001 — one household must never abort the batch
            logger.warning("anomaly_poller: slug=%s failed: %s", slug, exc)
    logger.info("anomaly_poller: polled %d household(s)", len(slugs))


def start() -> None:
    """Wire the periodic poll into an AsyncIOScheduler (Europe/Madrid) —
    called once from main.py's lifespan, mirrors advice_scheduler.start().
    No-op (degrades quietly) when EDGE_API_KEY is unset or APScheduler is
    missing — the dashboard must never depend on this to render."""
    if not settings.EDGE_API_KEY:
        logger.info("anomaly_poller disabled (EDGE_API_KEY not set)")
        return
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        logger.warning("APScheduler not installed — anomaly_poller disabled")
        return

    global _scheduler
    _scheduler = AsyncIOScheduler(timezone="Europe/Madrid")
    _scheduler.add_job(_poll_once, IntervalTrigger(minutes=settings.ANOMALY_POLL_INTERVAL_MIN),
                       id="anomaly_poll", replace_existing=True, misfire_grace_time=300)
    _scheduler.start()
    # Kick one immediate poll so the feed isn't empty for the first
    # ANOMALY_POLL_INTERVAL_MIN minutes after every restart/deploy.
    asyncio.create_task(_poll_once())
    logger.info("anomaly_poller started: every %d min (Europe/Madrid)",
               settings.ANOMALY_POLL_INTERVAL_MIN)


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
