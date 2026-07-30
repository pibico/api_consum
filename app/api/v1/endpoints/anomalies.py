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
from app.services import supply_points as supply_points_svc
from app.workers import anomaly_poller

router = APIRouter(prefix="/anomalies", tags=["anomalies"])


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
    return {"data": items, "meta": {"total": len(items), "slug": slug,
                                    "cold_start": cold and not items}}
