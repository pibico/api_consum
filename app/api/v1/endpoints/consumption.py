"""Consumption endpoints (F1) — org-scoped reads over the shared Timescale.

Every route resolves the caller via rbac.consum_context: members see the
union of their org's customer slugs; a `?customer=` narrows to one slug
(validated against the context). Peer services / superadmin pass any slug.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.v1.dependencies.rbac import ConsumContext, consum_context
from app.services import consumption, edge_client, exo_client

router = APIRouter(prefix="/consumption", tags=["consumption"])


async def _slugs(ctx: ConsumContext, customer: Optional[str]) -> list[str]:
    if customer:
        return [ctx.check_slug(customer)]
    if ctx.customer_slugs:
        return sorted(ctx.customer_slugs)
    if ctx.is_superadmin:
        # Superadmin browsing without a slug → fleet view (all households);
        # the customer selector in the UI narrows from there.
        return await consumption.all_slugs()
    if ctx.is_service:
        # Headless peer services must be explicit — refuse a global scan.
        raise HTTPException(400, detail="customer query param required for service callers")
    return []


@router.get("/context")
async def context(ctx: ConsumContext = Depends(consum_context)):
    """The caller's resolved context — drives the frontend (tier gates,
    customer selector, device list)."""
    slugs = sorted(ctx.customer_slugs) or (await consumption.all_slugs() if ctx.is_superadmin else [])
    return {
        "email": (ctx.user or {}).get("email"),
        "name": (ctx.user or {}).get("name"),
        "is_superadmin": ctx.is_superadmin,
        "tier": ctx.tier,
        "ai_enabled": ctx.ai_enabled,
        "role": ctx.role,
        "customers": slugs,
        "devices": await consumption.devices_for(slugs) if slugs else [],
    }


@router.get("/current")
async def current(customer: Optional[str] = Query(None),
                  ctx: ConsumContext = Depends(consum_context)):
    """Instantaneous power (whole house + per device)."""
    return await consumption.current_power(await _slugs(ctx, customer))


@router.get("/series")
async def series(start: str = Query(..., description="YYYY-MM-DD"),
                 end: str = Query(..., description="YYYY-MM-DD (inclusive)"),
                 bucket: str = Query("hour", pattern="^(hour|day)$"),
                 device: Optional[str] = Query(None, description="hostname"),
                 customer: Optional[str] = Query(None),
                 ctx: ConsumContext = Depends(consum_context)):
    """kWh per hour/day. Whole-house (EM channels) unless `device` given."""
    rows = await consumption.energy_series(await _slugs(ctx, customer), start, end,
                                           bucket=bucket, device=device)
    return {"bucket": bucket, "values": rows, "total_kwh": round(sum(r["kwh"] for r in rows), 2)}


@router.get("/summary")
async def summary(customer: Optional[str] = Query(None),
                  ctx: ConsumContext = Depends(consum_context)):
    """Today / 7d / 30d kWh + current power + PVPC-now context."""
    slugs = await _slugs(ctx, customer)
    data = await consumption.summary(slugs)
    data.update(await consumption.current_power(slugs) | {})
    # Price context (best-effort — dashboard shows '—' when api_exo is down)
    pvpc = await exo_client.pvpc_day("today")
    if pvpc and pvpc.get("prices"):
        from datetime import datetime
        h = datetime.now().hour
        row = next((p for p in pvpc["prices"] if p.get("hour") == h), None)
        if row:
            data["pvpc_now_eur_kwh"] = row.get("price_eur_kwh") or (
                (row.get("price_eur_mwh") or row.get("price") or 0) / 1000.0)
            data["pvpc_period"] = row.get("period")
    carbon = await exo_client.carbon_current()
    if carbon and carbon.get("intensity_gco2_kwh") is not None:
        data["carbon_gco2_kwh"] = carbon["intensity_gco2_kwh"]
        data["carbon_band"] = carbon.get("band")
    return data


@router.get("/devices")
async def devices(customer: Optional[str] = Query(None),
                  ctx: ConsumContext = Depends(consum_context)):
    """Devices of the caller's org (Timescale metadata + api_edge online state)."""
    slugs = await _slugs(ctx, customer)
    base = await consumption.devices_for(slugs)
    # Enrich with api_edge online flags (best-effort)
    online: dict[str, bool] = {}
    for slug in slugs:
        for d in (await edge_client.devices(slug)) or []:
            if d.get("hostname"):
                online[d["hostname"]] = bool(d.get("is_online"))
    for d in base:
        d["is_online"] = online.get(d["hostname"])
    return {"data": base}


# ---------------------------------------------------------------------------
# F2 — day/month views with PVPC cost (kWh_h × PVPC_h)
# ---------------------------------------------------------------------------

@router.get("/day")
async def day(date: str = Query(..., description="YYYY-MM-DD (local)"),
              device: Optional[str] = Query(None),
              customer: Optional[str] = Query(None),
              ctx: ConsumContext = Depends(consum_context)):
    """Hourly kWh for one local day joined with that day's PVPC:
    values[] = {hour, kwh, price_eur_kwh, period, cost_eur}."""
    slugs = await _slugs(ctx, customer)
    rows = await consumption.energy_series(slugs, date, date, bucket="hour", device=device)
    prices = await exo_client.pvpc_map(date, date)
    values = []
    total_kwh = total_cost = 0.0
    for r in rows:
        ts = r["ts"]
        if ts[:10] != date:          # bucket edges may spill into neighbours (UTC)
            continue
        hour = int(ts[11:13])
        p = prices.get((date, hour)) or {}
        price = p.get("price_eur_kwh")
        cost = round(r["kwh"] * price, 4) if price is not None else None
        total_kwh += r["kwh"]
        if cost is not None:
            total_cost += cost
        values.append({"hour": hour, "kwh": r["kwh"],
                       "price_eur_kwh": price, "period": p.get("period"),
                       "cost_eur": cost})
    return {"date": date, "values": values,
            "total_kwh": round(total_kwh, 2),
            "total_cost_eur": round(total_cost, 2) if values else 0,
            "avg_price_eur_kwh": round(total_cost / total_kwh, 4) if total_kwh > 0 and total_cost else None}


@router.get("/month")
async def month(month: str = Query(..., pattern=r"^\d{4}-\d{2}$", description="YYYY-MM"),
                device: Optional[str] = Query(None),
                customer: Optional[str] = Query(None),
                ctx: ConsumContext = Depends(consum_context)):
    """Daily kWh + exact daily cost (Σ hour kWh × hour PVPC) for one month."""
    import calendar
    from datetime import date as _date
    slugs = await _slugs(ctx, customer)
    y, m = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(y, m)[1]
    start, end = f"{month}-01", f"{month}-{last:02d}"
    today = _date.today().isoformat()
    if end > today:
        end = today if today >= start else start
    hourly = await consumption.energy_series(slugs, start, end, bucket="hour", device=device)
    prices = await exo_client.pvpc_map(start, end)
    days: dict[str, dict] = {}
    for r in hourly:
        d, h = r["ts"][:10], int(r["ts"][11:13])
        if d < start or d > end:
            continue
        entry = days.setdefault(d, {"date": d, "kwh": 0.0, "cost_eur": 0.0, "priced": False})
        entry["kwh"] += r["kwh"]
        p = prices.get((d, h))
        if p and p.get("price_eur_kwh") is not None:
            entry["cost_eur"] += r["kwh"] * p["price_eur_kwh"]
            entry["priced"] = True
    values = []
    for d in sorted(days):
        e = days[d]
        values.append({"date": d, "kwh": round(e["kwh"], 2),
                       "cost_eur": round(e["cost_eur"], 2) if e["priced"] else None})
    return {"month": month, "values": values,
            "total_kwh": round(sum(v["kwh"] for v in values), 2),
            "total_cost_eur": round(sum(v["cost_eur"] or 0 for v in values), 2)}
