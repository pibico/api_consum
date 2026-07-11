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
from app.api.v1.endpoints.consumption import _slugs
from app.services import oe3

router = APIRouter(prefix="/savings", tags=["savings"])

_cache: dict = {}
_TTL = 900  # 15 min


@router.get("/insights")
async def insights(customer: Optional[str] = Query(None),
                   device: Optional[str] = Query(None),
                   refresh: bool = Query(False),
                   ctx: ConsumContext = Depends(consum_context)):
    """The three OE3 insights in one call: shift (real vs optimal cost),
    thermal (consumption ~ HDD/CDD regression), window (tomorrow's
    cheap+sunny hours)."""
    slugs = await _slugs(ctx, customer)
    if not slugs:
        raise HTTPException(404, detail="no household in scope")
    key = ("|".join(sorted(slugs)), device or "")
    now = time.time()
    hit = _cache.get(key)
    if hit and now < hit[0] and not refresh:
        return hit[1]

    shift, thermal, window = await asyncio.gather(
        oe3.shift_analysis(slugs, device=device),
        oe3.thermal_analysis(slugs, device=device),
        oe3.green_window(),
    )
    out = {"shift": shift, "thermal": thermal, "window": window}
    _cache[key] = (now + _TTL, out)
    await oe3.persist_insights(key[0] + (f":{device}" if device else ""),
                               shift, thermal, window)
    return out
