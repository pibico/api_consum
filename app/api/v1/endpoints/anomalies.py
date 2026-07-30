"""Member-facing anomaly feed (Phase 2 E4 UI surfacing, 2026-07-29).

READ-ONLY: recent statistical anomaly events (api_edge's `anomaly_events`,
mig 024) for the caller's own household(s), served from the
`anomaly_poller` in-process cache (falls back to one live api_edge call on
a cold cache — e.g. right after a restart, before the poller's first tick
lands). No ack/resolve here — that stays in api_edge's admin console; a
member never mutates another tenant's write-path, and this app is the
read-only "product" side of the fence (see rbac.py's module docstring).

The fleet has <8 weeks of history as of this writing (Phase 2 cold-start),
so this will mostly return an empty list — `cold_start` in the response
lets the UI tell "no anomalies yet" apart from "poller hasn't reached this
household yet", though both render the same graceful empty state.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.v1.dependencies.rbac import ConsumContext, consum_context
from app.api.v1.endpoints.consumption import _slugs, _sp
from app.services import absences as absences_svc
from app.services import consumption, supply_points as supply_points_svc
from app.workers import anomaly_poller

router = APIRouter(prefix="/anomalies", tags=["anomalies"])


async def _tag_expected_absence(items: list, customer_id: Optional[str]) -> None:
    """Optional enrichment (2026-07-30 absence prior, spec: "tag suppressed
    ones... in the feed rather than dropping silently") — mutates each item
    in place with `expected_absence: bool`. Unlike the EMAIL dispatch
    (`anomaly_poller._dispatch_new_anomalies`), the read-only feed NEVER
    drops a row over this — a LOW anomaly inside a declared absence still
    shows, just tagged as expected; HIGH anomalies are never tagged."""
    if not items or not customer_id:
        return
    points = await supply_points_svc.ensure_for_slugs([customer_id])
    dev_to_sp: dict = {}
    for p in points:
        for dev, ch in await supply_points_svc.subtree(p):
            dev_to_sp[(dev, ch)] = p["id"]
    from datetime import datetime as _dt, timezone as _tz
    for a in items:
        a["expected_absence"] = False
        observed, expected = a.get("value_observed"), a.get("value_expected")
        if observed is None or expected is None or observed >= expected:
            continue   # only LOW anomalies can ever be "expected" by an absence
        ts_raw = a.get("ts")
        if not ts_raw:
            continue
        try:
            ts = _dt.fromisoformat(ts_raw)
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=_tz.utc)
        sp_id = dev_to_sp.get((a.get("device_id"), a.get("channel")))
        hit = await absences_svc.active_at(customer_id, sp_id, ts)
        if hit:
            a["expected_absence"] = True


@router.get("")
async def list_anomalies(customer: Optional[str] = Query(None),
                         supply: Optional[int] = Query(None),
                         limit: int = Query(20, le=100),
                         ctx: ConsumContext = Depends(consum_context)):
    slugs = await _slugs(ctx, customer)
    if not slugs:
        raise HTTPException(404, detail="no household in scope")
    sp = await _sp(slugs, supply)
    slug = slugs[0]

    hit = anomaly_poller.cached(slug)
    cold = hit["items"] is None
    items = hit["items"]
    if cold:
        items = await anomaly_poller.fetch_live(slug)

    if sp:
        # Multi-CUPS awareness (?supply=): narrow to this installation's
        # device subtree — anomaly rows carry device_id, not a supply_point
        # id (anomaly_events predates F3's supply_points model).
        subtree_devices = {d for d, _ch in await supply_points_svc.subtree(sp)}
        if subtree_devices:
            items = [a for a in items if a.get("device_id") in subtree_devices]

    items = sorted(items, key=lambda a: a.get("ts") or "", reverse=True)[:limit]
    ids = await consumption._slugs_to_ids([slug])
    await _tag_expected_absence(items, ids.get(slug))
    return {"data": items, "meta": {"total": len(items), "slug": slug,
                                    "cold_start": cold and not items}}
