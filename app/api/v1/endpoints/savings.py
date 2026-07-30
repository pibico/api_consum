"""Savings endpoints (F3) — the OE3 cross-analysis, org-scoped like
/consumption. One aggregate route feeds the whole Ahorro page; results are
cached in-process 15 min per scope (the underlying window queries span
30-45 days of hourly data) and snapshotted daily to SQLite for F4's AI.
"""
from __future__ import annotations

import asyncio
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.v1.dependencies.rbac import ConsumContext, consum_context
from app.api.v1.endpoints.consumption import _slugs, _sp
from app.services import consumption, oe3

router = APIRouter(prefix="/savings", tags=["savings"])

_cache: dict = {}
_TTL = 900  # 15 min


@router.get("/insights")
async def insights(customer: Optional[str] = Query(None),
                   device: Optional[str] = Query(None),
                   supply: Optional[int] = Query(None),
                   refresh: bool = Query(False),
                   ctx: ConsumContext = Depends(consum_context)):
    """The three OE3 insights in one call: shift (real vs optimal cost),
    thermal (consumption ~ HDD/CDD regression, equipment-gated), window
    (tomorrow's cheap+sunny hours)."""
    slugs = await _slugs(ctx, customer)
    if not slugs:
        raise HTTPException(404, detail="no household in scope")
    sp = await _sp(slugs, supply)
    # Equipment gate (gap fix 2026-07-30, same contract as the forecast card
    # — hard requirement 2026-07-29): thermal_analysis must never attribute
    # consumption to HDD/CDD the household has no electric heating/cooling
    # to produce. Fetched BEFORE the cache key so an equipment change busts
    # the cache immediately instead of waiting out the 15-min TTL.
    comfort = await consumption.comfort_flex_for(slugs, sp=sp)
    equipment = comfort.get("equipment")
    key = ("|".join(sorted(slugs)), device or "", supply or 0, comfort.get("updated_at"))
    now = time.time()
    hit = _cache.get(key)
    if hit and now < hit[0] and not refresh:
        return hit[1]

    shift, thermal, window, achieved, appliances = await asyncio.gather(
        oe3.shift_analysis(slugs, device=device, sp=sp),
        oe3.thermal_analysis(slugs, device=device, sp=sp, equipment=equipment),
        oe3.green_window(),
        oe3.achieved_savings(slugs, device=device, sp=sp),
        oe3.appliance_costs(slugs, sp=sp),
    )
    out = {"shift": shift, "thermal": thermal, "window": window,
           "achieved": achieved, "appliances": appliances}
    _cache[key] = (now + _TTL, out)
    await oe3.persist_insights(key[0] + (f":{device}" if device else ""),
                               shift, thermal, window)
    return out
