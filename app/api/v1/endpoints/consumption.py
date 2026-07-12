"""Consumption endpoints (F1) — org-scoped reads over the shared Timescale.

Every route resolves the caller via rbac.consum_context: members see the
union of their org's customer slugs; a `?customer=` narrows to one slug
(validated against the context). Peer services / superadmin pass any slug.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.v1.dependencies.rbac import (ConsumContext, consum_context,
                                          require_role, require_tier)
from app.services import consumption, edge_client, exo_client, pricing
from app.services import contracts as contracts_svc

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


@router.get("/topology")
async def topology(customer: Optional[str] = Query(None),
                   ctx: ConsumContext = Depends(consum_context)):
    """The household's PLC wiring tree with LIVE watts (proxied from the CM4
    via api_edge). `{status:"offline"}` when the PLC is unreachable — the
    frontend then falls back to the persisted sensors topology + cloud power."""
    slugs = await _slugs(ctx, customer)
    if not slugs:
        return {"status": "offline", "reason": "no_household"}
    data = await edge_client.topology(slugs[0])
    if data and data.get("status") == "ok" and data.get("root"):
        return data
    # PLC unreachable → rebuild the tree from the persisted sensors topology.
    return await consumption.topology_from_sensors(slugs)


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


@router.get("/power-peak")
async def power_peak(date: str = Query(..., description="YYYY-MM-DD (local)"),
                     device: Optional[str] = Query(None),
                     customer: Optional[str] = Query(None),
                     ctx: ConsumContext = Depends(consum_context)):
    """Peak power (W) reached in each hour of the day — the household demand
    curve (relevant to the 2.0TD power term)."""
    slugs = await _slugs(ctx, customer)
    rows = await consumption.power_peak_hourly(slugs, date, device=device)
    peak = max((r["peak_w"] for r in rows), default=0)
    return {"date": date, "values": rows, "peak_w": peak}


@router.get("/summary")
async def summary(customer: Optional[str] = Query(None),
                  ctx: ConsumContext = Depends(consum_context)):
    """Today / 7d / 30d kWh + current power + PVPC-now context."""
    slugs = await _slugs(ctx, customer)
    data = await consumption.summary(slugs)
    data.update(await consumption.current_power(slugs) | {})
    # Price context (best-effort — dashboard shows '—' when api_exo is down).
    # pvpc_now_* stays for backward compat; price_now_* follows the contract.
    pvpc = await exo_client.pvpc_day("today")
    if pvpc and pvpc.get("prices"):
        from datetime import datetime
        h = datetime.now().hour
        row = next((p for p in pvpc["prices"] if p.get("hour") == h), None)
        if row:
            data["pvpc_now_eur_kwh"] = row.get("price_eur_kwh") or (
                (row.get("price_eur_mwh") or row.get("price") or 0) / 1000.0)
            data["pvpc_period"] = row.get("period")
    from datetime import date as _date
    today = _date.today().isoformat()
    cid, _cl = await contracts_svc.resolve_for_slugs(slugs, today, today)
    contract = await contracts_svc.get_active(cid, today) if cid else None
    now_price = await pricing.price_now(contract)
    if now_price:
        data["price_now_eur_kwh"] = now_price["price_eur_kwh"]
        data["price_period"] = now_price["period"]
        data["price_source"] = now_price["source"]
    carbon = await exo_client.carbon_current()
    if carbon and carbon.get("intensity_gco2_kwh") is not None:
        data["carbon_gco2_kwh"] = carbon["intensity_gco2_kwh"]
        data["carbon_band"] = carbon.get("band")
    return data


@router.get("/sensors")
async def sensors(customer: Optional[str] = Query(None),
                  ctx: ConsumContext = Depends(consum_context)):
    """Reporting sensor device_ids (last 30 d) — feeds the per-device filter
    in Consumo/Ahorro. Gateways live in /devices; they never report power."""
    return {"data": await consumption.sensor_devices(await _slugs(ctx, customer))}


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


@router.get("/environment")
async def environment(customer: Optional[str] = Query(None),
                      ctx: ConsumContext = Depends(consum_context)):
    """Panel v2 — the household's exogenous environment, personalized and
    assembled server-side from api_exo (the browser never holds the exo key):
    PVPC curves today/tomorrow, AEMET weather, solar forecast, grid carbon
    and the OE3 green window. Location: household coords (v1 = defaults;
    per-customer coords come with the onboarding flow)."""
    import asyncio
    from datetime import date as _date, timedelta as _td

    from app.core.config import settings
    from app.services import oe3

    slugs = await _slugs(ctx, customer)
    loc = await consumption.location_for(slugs)
    lat = loc["lat"] if loc else settings.DEFAULT_LAT
    lon = loc["lon"] if loc else settings.DEFAULT_LON
    # Rooftop-PV config scales the solar estimate to the real installation
    # (mig 003); absent → api_exo's 3 kWp default.
    solar_cfg = await consumption.solar_config_for(slugs) or {}
    (today, tomorrow, omie_today, omie_tomorrow, carbon,
     weather, obs, solar, sun, window, wind) = await asyncio.gather(
        exo_client.pvpc_day("today"),
        exo_client.pvpc_day("tomorrow"),
        exo_client.omie_day("today"),
        exo_client.omie_day("tomorrow"),
        exo_client.carbon_current(),
        exo_client.weather_forecast(lat, lon),
        exo_client.weather_observations(lat, lon),
        exo_client.solar_forecast(lat, lon,
                                  peak_kwp=solar_cfg.get("peak_kwp"),
                                  tilt=solar_cfg.get("tilt"),
                                  azimuth=solar_cfg.get("azimuth"),
                                  loss=solar_cfg.get("loss")),
        exo_client.daylight(lat, lon),
        oe3.green_window(lat, lon),
        exo_client.wind_forecast(lat, lon),
    )

    def _kwh(r):
        return r.get("price_eur_kwh") or ((r.get("price_eur_mwh") or 0) / 1000.0)

    def _prices(payload):
        out = []
        for r in (payload or {}).get("prices") or []:
            if r.get("hour") is None:
                continue
            out.append({"hour": int(r["hour"]), "price_eur_kwh": _kwh(r),
                        "period": r.get("period")})
        return out

    _band = pricing._band   # 2.0TD static calendar (shared fallback)

    def _omie_points(payload, pvpc_rows, day_date):
        # OMIE day-ahead is quarter-hourly (15-min MTU) — keep the full 96
        # points so the chart hover shows the exact quarter price. The spot
        # has no bands of its own, but the 2.0TD access-toll band applies by
        # calendar — copy it from the same day's PVPC rows (fallback: static
        # schedule) so the chart colours match PVPC's.
        by_hour = {p["hour"]: p.get("period") for p in pvpc_rows or []}
        weekend = day_date.weekday() >= 5
        pts = []
        for r in (payload or {}).get("prices") or []:
            dl = str(r.get("datetime_local") or "")
            if len(dl) >= 16:
                h = int(dl[11:13])
                pts.append({"time": dl[11:16],
                            "price_eur_kwh": round(_kwh(r), 5),
                            "period": by_hour.get(h) or _band(h, weekend)})
        pts.sort(key=lambda p: p["time"])
        return pts

    days = (weather or {}).get("forecast") or []
    now_wx = (obs or {}).get("data") or None
    # Full 48 h production curve (api_exo solar page parity): kW per hour
    # with cloud cover, so the card chart carries the same detail.
    solar_hours = [
        {"ts": row.get("ts"), "ghi": round(float(row.get("ghi") or 0)),
         "cloud_pct": row.get("cloud_pct"),
         "p_kw": round(float(row.get("p_kw") or 0), 3),
         "is_day": bool(row.get("is_day"))}
        for row in (solar or {}).get("hourly") or []
    ]
    sun_today = (((sun or {}).get("daily")) or [{}])[0]
    return {
        "location": {"lat": lat, "lon": lon,
                     "municipality": (loc or {}).get("municipality")
                     or (days[0].get("municipality") if days else None)},
        "pvpc": {"today": _prices(today), "tomorrow": _prices(tomorrow) or None},
        "omie": {"today": _omie_points(omie_today, _prices(today), _date.today()),
                 "tomorrow": _omie_points(omie_tomorrow, _prices(tomorrow),
                                          _date.today() + _td(days=1)) or None},
        "carbon": {"intensity_gco2_kwh": (carbon or {}).get("intensity_gco2_kwh"),
                   "band": (carbon or {}).get("band")},
        "weather": {
            "now": ({k: now_wx.get(k) for k in ("temperature", "feels_like",
                                                "humidity", "description",
                                                "station")} if now_wx else None),
            "days": [{k: d.get(k) for k in ("date", "temp_max", "temp_min",
                                            "description", "precipitation_prob")}
                     for d in days[:7]],
        },
        "solar": {"today_kwh": (solar or {}).get("today_kwh"),
                  "tomorrow_kwh": (solar or {}).get("tomorrow_kwh"),
                  "peak_kwp": ((solar or {}).get("config") or {}).get("peak_kwp"),
                  "peak_window": (solar or {}).get("peak_window"),
                  "hourly": solar_hours,
                  "sun": {"sunrise": sun_today.get("sunrise"),
                          "sunset": sun_today.get("sunset"),
                          "daylight_seconds": sun_today.get("daylight_seconds")}},
        "window": window,
        "wind": {"hourly": (wind or {}).get("hourly") or [],
                 "source": (wind or {}).get("source") or "Open-Meteo"},
        # Data provenance per card (transparency requirement): pass the
        # upstream `source` fields through instead of hardcoding names.
        "sources": {
            "pvpc": (today or {}).get("source") or "ESIOS",
            "omie": (omie_today or {}).get("source") or "ESIOS",
            "carbon": ((carbon or {}).get("source") or "REE/ESIOS").split(" ")[0],
            "weather": (weather or {}).get("source") or "AEMET",
            "weather_station": ((obs or {}).get("data") or {}).get("station"),
            "solar": (solar or {}).get("source") or "Open-Meteo",
        },
    }


# ---------------------------------------------------------------------------
# F2 — day/month views with PVPC cost (kWh_h × PVPC_h)
# ---------------------------------------------------------------------------

@router.get("/day")
async def day(date: str = Query(..., description="YYYY-MM-DD (local)"),
              device: Optional[str] = Query(None),
              customer: Optional[str] = Query(None),
              ctx: ConsumContext = Depends(consum_context)):
    """One local day. Settlement (values[]/totals) is HOURLY — the hourly
    meter delta × hourly quarter-mean price, exactly what the retailer bills
    and byte-identical to the pre-quarter behavior. The 15-min quarters[] is
    a DISPLAY curve for the Panel: its shape comes from the real 15-min meter,
    but each hour's four quarters are rescaled to sum to that hour's exact
    settlement kWh (a cumulative counter bucketed at 15 min drops the
    between-bucket energy, so raw quarter deltas would undercount the hour)."""
    slugs = await _slugs(ctx, customer)
    hourly = await consumption.energy_series(slugs, date, date, bucket="hour", device=device)
    quarter = await consumption.energy_series(slugs, date, date, bucket="quarter", device=device)
    pmap, price_source = await pricing.price_map_for_slugs(slugs, date, date)
    hmap = pricing.hourly_rollup(pmap)

    # Settlement (hourly) — the source of truth for totals & the retailer bill.
    kwh_by_hour: dict[int, float] = {}
    for r in hourly:
        if r["ts"][:10] != date:      # bucket edges may spill into neighbours (UTC)
            continue
        kwh_by_hour[int(r["ts"][11:13])] = r["kwh"]
    values = []
    total_kwh = total_cost = 0.0
    for hour in sorted(kwh_by_hour):
        kwh = kwh_by_hour[hour]
        p = hmap.get((date, hour)) or {}
        price = p.get("price_eur_kwh")
        cost = round(kwh * price, 4) if price is not None else None
        total_kwh += kwh
        if cost is not None:
            total_cost += cost
        values.append({"hour": hour, "kwh": kwh,
                       "price_eur_kwh": round(price, 5) if price is not None else None,
                       "period": p.get("period"), "cost_eur": cost})

    # Display curve (quarter) — rescale each hour's quarters to the settlement
    # kWh so the bars sum to the hourly total shown everywhere else.
    raw_q: dict[int, list] = {}
    for r in quarter:
        if r["ts"][:10] != date:
            continue
        raw_q.setdefault(int(r["ts"][11:13]), []).append(r)
    quarters = []
    for hour in sorted(raw_q):
        qs = raw_q[hour]
        raw_sum = sum(x["kwh"] for x in qs)
        target = kwh_by_hour.get(hour, 0.0)
        factor = (target / raw_sum) if raw_sum > 0 else 0.0
        for x in qs:
            hhmm = x["ts"][11:16]
            kwh = round(x["kwh"] * factor, 3) if raw_sum > 0 else round(target / len(qs), 3)
            p = pmap.get((date, hhmm)) or {}
            price = p.get("price_eur_kwh")
            quarters.append({"time": hhmm, "kwh": kwh,
                             "price_eur_kwh": price, "period": p.get("period"),
                             "cost_eur": round(kwh * price, 4) if price is not None else None})
    return {"date": date, "values": values, "quarters": quarters,
            "price_source": price_source,
            "total_kwh": round(total_kwh, 2),
            "total_cost_eur": round(total_cost, 2) if values else 0,
            "avg_price_eur_kwh": round(total_cost / total_kwh, 4) if total_kwh > 0 and total_cost else None}


@router.get("/month")
async def month(month: str = Query(..., pattern=r"^\d{4}-\d{2}$", description="YYYY-MM"),
                device: Optional[str] = Query(None),
                customer: Optional[str] = Query(None),
                ctx: ConsumContext = Depends(consum_context)):
    """Daily kWh + exact daily cost (Σ hour kWh × hour tariff price — active
    contract, PVPC fallback) for one month."""
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
    prices, price_source = await pricing.hourly_price_map_for_slugs(slugs, start, end)
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
    return {"month": month, "values": values, "price_source": price_source,
            "total_kwh": round(sum(v["kwh"] for v in values), 2),
            "total_cost_eur": round(sum(v["cost_eur"] or 0 for v in values), 2)}


# ---------------------------------------------------------------------------
# F4 — PRO tier: bill forecast, tariff-band breakdown, CSV export
# ---------------------------------------------------------------------------

async def _month_hourly_costed(slugs, month: str, device):
    """Shared helper: the month's hourly rows joined with the household's
    tariff at HOURLY SETTLEMENT prices (active contract, PVPC fallback)."""
    import calendar
    from datetime import date as _date
    y, m = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(y, m)[1]
    start, end = f"{month}-01", f"{month}-{last:02d}"
    today = _date.today().isoformat()
    if end > today:
        end = today if today >= start else start
    hourly = await consumption.energy_series(slugs, start, end, bucket="hour", device=device)
    prices, price_source = await pricing.hourly_price_map_for_slugs(slugs, start, end)
    rows = []
    for r in hourly:
        d, h = r["ts"][:10], int(r["ts"][11:13])
        if d < start or d > end:
            continue
        p = prices.get((d, h)) or {}
        rows.append({"date": d, "hour": h, "kwh": r["kwh"],
                     "price_eur_kwh": p.get("price_eur_kwh"),
                     "period": p.get("period")})
    return rows, last, price_source


@router.get("/forecast-month")
async def forecast_month(customer: Optional[str] = Query(None),
                         device: Optional[str] = Query(None),
                         ctx: ConsumContext = Depends(require_tier("pro"))):
    """PRO — month-end kWh/€ projection: month-to-date + remaining days at
    the recent daily average (last 14 complete days)."""
    from datetime import date as _date, timedelta
    slugs = await _slugs(ctx, customer)
    today = _date.today()
    month = today.strftime("%Y-%m")
    rows, last_day, price_source = await _month_hourly_costed(slugs, month, device)

    mtd_kwh = sum(r["kwh"] for r in rows)
    mtd_cost = sum(r["kwh"] * r["price_eur_kwh"] for r in rows
                   if r["price_eur_kwh"] is not None)
    # Recent daily averages (14 complete days, may span into previous month)
    ref_start = (today - timedelta(days=14)).isoformat()
    ref_end = (today - timedelta(days=1)).isoformat()
    ref = await consumption.energy_series(slugs, ref_start, ref_end,
                                          bucket="hour", device=device)
    ref_prices, _ = await pricing.hourly_price_map_for_slugs(slugs, ref_start, ref_end)
    daily: dict = {}
    for r in ref:
        d, h = r["ts"][:10], int(r["ts"][11:13])
        e = daily.setdefault(d, {"kwh": 0.0, "cost": 0.0})
        e["kwh"] += r["kwh"]
        p = ref_prices.get((d, h))
        if p and p.get("price_eur_kwh") is not None:
            e["cost"] += r["kwh"] * p["price_eur_kwh"]
    days_ref = [v for v in daily.values() if v["kwh"] > 0]
    avg_kwh = sum(v["kwh"] for v in days_ref) / len(days_ref) if days_ref else 0.0
    avg_cost = sum(v["cost"] for v in days_ref) / len(days_ref) if days_ref else 0.0
    remaining = max(last_day - today.day, 0) + 1   # today still accruing

    return {"month": month, "day_of_month": today.day, "days_in_month": last_day,
            "price_source": price_source,
            "mtd_kwh": round(mtd_kwh, 2), "mtd_cost_eur": round(mtd_cost, 2),
            "ref_days": len(days_ref),
            "avg_day_kwh": round(avg_kwh, 2), "avg_day_cost_eur": round(avg_cost, 2),
            "forecast_kwh": round(mtd_kwh + avg_kwh * remaining, 1),
            "forecast_cost_eur": round(mtd_cost + avg_cost * remaining, 2)}


@router.get("/bands")
async def bands(month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
                customer: Optional[str] = Query(None),
                device: Optional[str] = Query(None),
                ctx: ConsumContext = Depends(require_tier("pro"))):
    """PRO — kWh and € split by tariff band (P1/P2/P3) for one month."""
    slugs = await _slugs(ctx, customer)
    rows, _, price_source = await _month_hourly_costed(slugs, month, device)
    out = {p: {"period": p, "kwh": 0.0, "cost_eur": 0.0} for p in ("P1", "P2", "P3")}
    total_kwh = 0.0
    for r in rows:
        total_kwh += r["kwh"]
        p = r["period"]
        if p in out:
            out[p]["kwh"] += r["kwh"]
            if r["price_eur_kwh"] is not None:
                out[p]["cost_eur"] += r["kwh"] * r["price_eur_kwh"]
    values = []
    for p in ("P1", "P2", "P3"):
        e = out[p]
        values.append({"period": p, "kwh": round(e["kwh"], 2),
                       "cost_eur": round(e["cost_eur"], 2),
                       "share_pct": round(e["kwh"] / total_kwh * 100, 1) if total_kwh else 0.0})
    return {"month": month, "values": values, "price_source": price_source,
            "total_kwh": round(total_kwh, 2)}


@router.get("/export.csv")
async def export_csv(start: str = Query(..., description="YYYY-MM-DD"),
                     end: str = Query(..., description="YYYY-MM-DD (inclusive, max 92 days)"),
                     customer: Optional[str] = Query(None),
                     device: Optional[str] = Query(None),
                     ctx: ConsumContext = Depends(require_tier("pro"))):
    """PRO — hourly CSV: date,hour,kwh,price_eur_kwh,period,cost_eur,source."""
    from datetime import date as _date
    from fastapi.responses import PlainTextResponse
    try:
        d0, d1 = _date.fromisoformat(start), _date.fromisoformat(end)
    except ValueError:
        raise HTTPException(400, detail="invalid date format")
    if (d1 - d0).days > 92 or d1 < d0:
        raise HTTPException(400, detail="range must be 1-92 days")
    slugs = await _slugs(ctx, customer)
    hourly = await consumption.energy_series(slugs, start, end, bucket="hour", device=device)
    prices, _ = await pricing.hourly_price_map_for_slugs(slugs, start, end)
    lines = ["date,hour,kwh,price_eur_kwh,period,cost_eur,source"]
    for r in hourly:
        d, h = r["ts"][:10], int(r["ts"][11:13])
        if d < start or d > end:
            continue
        p = prices.get((d, h)) or {}
        price = p.get("price_eur_kwh")
        cost = round(r["kwh"] * price, 4) if price is not None else ""
        lines.append(f"{d},{h},{r['kwh']},{price if price is not None else ''},"
                     f"{p.get('period') or ''},{cost},{p.get('source') or ''}")
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/csv",
                             headers={"Content-Disposition":
                                      f"attachment; filename=consumo_{start}_{end}.csv"})


# ── Rooftop-PV config (mig 003): the installed kWp that scales the Panel
#    solar estimate. Read is org-scoped; write needs editor+ of the household.
class SolarConfigIn(BaseModel):
    customer: Optional[str] = None
    peak_kwp: float = Field(..., ge=0, le=100)
    tilt: Optional[float] = Field(None, ge=0, le=90)
    azimuth: Optional[float] = Field(None, ge=0, le=360)
    loss: Optional[float] = Field(None, ge=0, le=50)


@router.get("/solar-config")
async def get_solar_config(customer: Optional[str] = Query(None),
                           ctx: ConsumContext = Depends(consum_context)):
    """The household's rooftop-PV config; `peak_kwp` null → api_exo 3 kWp
    default applies."""
    slugs = await _slugs(ctx, customer)
    cfg = await consumption.solar_config_for(slugs)
    return cfg or {"peak_kwp": None, "tilt": None, "azimuth": None, "loss": None}


@router.put("/solar-config")
async def put_solar_config(body: SolarConfigIn,
                           ctx: ConsumContext = Depends(consum_context)):
    """Set the household's installed kWp (+ optional orientation). This is a
    self-service display preference (it only scales the solar ESTIMATE, not
    billing), so ANY authenticated household member may set it — not gated to
    editor like contracts. `check_slug` still confines writes to own households;
    a peer service key (read-only) is refused."""
    if ctx.is_service and not ctx.is_superadmin:
        raise HTTPException(403, detail="read-only service key")
    slugs = await _slugs(ctx, body.customer)
    if not slugs:
        raise HTTPException(400, detail="no household in scope")
    slug = slugs[0]
    ctx.check_slug(slug)
    return await consumption.set_solar_config(
        slug, body.peak_kwp, tilt=body.tilt, azimuth=body.azimuth,
        loss=body.loss, updated_by=(ctx.user or {}).get("email"))
