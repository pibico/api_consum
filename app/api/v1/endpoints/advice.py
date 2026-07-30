"""Explainable forecast + advice email — opt-in + manual trigger (S8) and the
member-facing forecast READ (UI surfacing, 2026-07-29).

Member-facing: GET/POST /advice/prefs (household consent toggle, settings
slide panel); GET /advice/forecast (the dashboard forecast card — same
pipeline as the S7 email, no opt-in required just to VIEW it). Superadmin-
only: POST /advice/trigger (manual QA send, kept behind require_role since
the spec's "open micro-decision" said keep-or-drop after launch — cheap to
keep, useful for verifying end-to-end without waiting for the cron).
"""
from __future__ import annotations

import time
from datetime import date as _date
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from app.api.v1.dependencies.rbac import ConsumContext, consum_context, require_role
from app.api.v1.endpoints.consumption import _slugs, _sp
from app.services import advice_prefs, consumption

router = APIRouter(prefix="/advice", tags=["advice"])

_forecast_cache: dict = {}
_FORECAST_TTL = 900  # 15 min — every miss may narrate via the LLM (cost + latency)


@router.get("/forecast")
async def forecast(cadence: str = Query("daily", pattern="^(daily|weekly)$"),
                   customer: Optional[str] = Query(None),
                   supply: Optional[int] = Query(None),
                   lang: str = Query("es", pattern="^(es|en)$"),
                   refresh: bool = Query(False),
                   ctx: ConsumContext = Depends(consum_context)):
    """The dashboard forecast card's data: same forecast_explained +
    AdviceSkill.narrate pipeline as the S7 advice email, cached 15 min per
    household·cadence·supply·day (a cache miss narrates via the LLM — this
    must not run on every page paint). Viewing never requires the email
    opt-in (that only gates delivery, not visibility)."""
    from app.workers import advice_scheduler

    slugs = await _slugs(ctx, customer)
    if not slugs:
        raise HTTPException(404, detail="no household in scope")
    sp = await _sp(slugs, supply)
    slug = slugs[0]
    # Hard requirement 2026-07-29: gate the forecast/advice on the
    # household's actual electric equipment (comfort_flex, mig 015) — never
    # let HDD/CDD explain consumption, or the advice suggest heating/cooling
    # actions, for a site that doesn't have that equipment. Fetched BEFORE
    # the cache key so an equipment change busts the cache immediately
    # instead of waiting out the 15-min TTL.
    comfort = await consumption.comfort_flex_for(slugs, sp=sp)
    equipment = comfort.get("equipment")
    key = (slug, cadence, sp.get("id") if sp else 0, _date.today().isoformat(), comfort.get("updated_at"))
    now = time.time()
    hit = _forecast_cache.get(key)
    if hit and now < hit[0] and not refresh:
        return hit[1]
    ids = await consumption._slugs_to_ids([slug])
    cid = ids.get(slug)
    prefs = (await advice_prefs.get(cid)) if cid else None
    region = (prefs or {}).get("region")
    result = await advice_scheduler.forecast_for_member(
        slug, cadence, lang=lang, region=region, sp=sp, user_email=(ctx.user or {}).get("email"),
        equipment=equipment)
    _forecast_cache[key] = (now + _FORECAST_TTL, result)
    return result


@router.get("/prefs")
async def get_prefs(customer: Optional[str] = Query(None),
                    ctx: ConsumContext = Depends(consum_context)):
    slugs = await _slugs(ctx, customer)
    if not slugs:
        raise HTTPException(404, detail="no household in scope")
    ids = await consumption._slugs_to_ids(slugs[:1])
    cid = next(iter(ids.values()), None)
    if not cid:
        raise HTTPException(404, detail="household not found")
    prefs = await advice_prefs.get(cid)
    return prefs or {"customer_id": cid, "opt_in": False, "recipient_email": None,
                     "lang": "es", "region": None}


@router.post("/prefs")
async def set_prefs(opt_in: bool = Body(..., embed=True),
                    lang: str = Body("es", embed=True),
                    region: Optional[str] = Body(None, embed=True),
                    customer: Optional[str] = Body(None, embed=True),
                    ctx: ConsumContext = Depends(consum_context)):
    slugs = await _slugs(ctx, customer)
    if not slugs:
        raise HTTPException(404, detail="no household in scope")
    ids = await consumption._slugs_to_ids(slugs[:1])
    cid = next(iter(ids.values()), None)
    if not cid:
        raise HTTPException(404, detail="household not found")
    email = (ctx.user or {}).get("email")
    if opt_in and not email:
        raise HTTPException(400, detail="no recipient email on this account")
    return await advice_prefs.set_opt_in(
        cid, opt_in, recipient_email=email if opt_in else None,
        lang=lang, region=region, updated_by=email)


@router.post("/trigger")
async def trigger(customer: str = Body(..., embed=True),
                  cadence: str = Body("daily", embed=True, pattern="^(daily|weekly)$"),
                  dry_run: bool = Body(True, embed=True),
                  ctx: ConsumContext = Depends(require_role("admin"))):
    """Manual QA trigger — superadmin/org-admin only. `dry_run=True` (default)
    builds the forecast+narrative+email context WITHOUT sending or touching
    the cooldown ledger; `dry_run=False` sends for real (subject to
    EMAIL_ADVICE_ENABLED, cooldown, daily cap)."""
    from app.workers import advice_scheduler

    slug = ctx.check_slug(customer)
    ids = await consumption._slugs_to_ids([slug])
    cid = ids.get(slug)
    if not cid:
        raise HTTPException(404, detail="household not found")
    prefs = await advice_prefs.get(cid) or {}
    recipient = prefs.get("recipient_email") or (ctx.user or {}).get("email")
    household = {"slug": slug, "customer_id": cid, "recipient_email": recipient,
                "lang": prefs.get("lang") or "es", "region": prefs.get("region")}
    return await advice_scheduler.run_for_household(household, cadence, dry_run=dry_run)
