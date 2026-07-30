"""api_exo client — exogenous variables for the dashboard/OE3 (F1).

api_exo's data reads are auth-gated (REQUIRE_READ_AUTH=true): every call
presents X-API-Key = settings.EXO_API_KEY (api_exo's regular service key —
non-admin). Responses carry their own cache metadata; we keep a small local
TTL cache to avoid hammering across dashboard autorefreshes.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

import httpx

from app.core import http_client
from app.core.config import settings

logger = logging.getLogger("consum.exo")

_cache: Dict[str, tuple[float, Any]] = {}
_TTL = 300  # 5 min — matches the dashboard autorefresh cadence


async def _get(path: str, ttl: int = _TTL) -> Optional[dict]:
    now = time.time()
    hit = _cache.get(path)
    if hit and now < hit[0]:
        return hit[1]
    url = f"{settings.EXO_BASE_URL.rstrip('/')}/api/v1{path}"
    try:
        client = http_client.get_client()
        r = await client.get(url, headers={"X-API-Key": settings.EXO_API_KEY}, timeout=15.0)
        if r.status_code != 200:
            logger.warning("api_exo %s -> %s", path, r.status_code)
            return None
        data = r.json()
        _cache[path] = (now + ttl, data)
        return data
    except httpx.RequestError as e:
        logger.error("api_exo unreachable (%s): %s", path, e)
        return None


async def pvpc_day(day: str = "today") -> Optional[dict]:
    """Hourly PVPC for today|tomorrow → {prices: [{hour, price_eur_kwh|price_eur_mwh, period}]}."""
    return await _get(f"/prices/pvpc/{day}")


async def pvpc_range(start: str, end: str) -> Optional[dict]:
    return await _get(f"/prices/pvpc?start_date={start}&end_date={end}", ttl=3600)


async def degree_days(lat: float, lon: float, start: str, end: str) -> Optional[dict]:
    """Daily HDD/CDD range → {days: [{date, temp_mean, hdd, cdd}]} (OE3)."""
    return await _get(
        f"/climate/degree-days?lat={lat}&lon={lon}&start_date={start}&end_date={end}",
        ttl=3600,
    )


async def tariff_components(access_tariff: str = "2.0TD") -> Optional[dict]:
    """Regulated indexed-price components from api_exo (SSOT, exogenous.
    tariff_components): {components: {SA: {bands, unit, source, verified}, …},
    all_verified}. 1 h TTL — they change by BOE order, not intraday. None →
    callers fall back to the vendored defaults (tariff_components.py)."""
    return await _get(f"/tariffs/components?access_tariff={access_tariff}", ttl=3600)


async def solar_forecast(lat: float, lon: float,
                         peak_kwp: Optional[float] = None,
                         tilt: Optional[float] = None,
                         azimuth: Optional[float] = None,
                         loss: Optional[float] = None) -> Optional[dict]:
    """PV production forecast. Optional installation params (kWp/tilt/azimuth/
    loss) override api_exo's defaults so the estimate reflects the real rooftop;
    omitted params fall back to api_exo (3 kWp, 30° tilt, south, 14% loss)."""
    qs = f"/weather/solar-forecast?lat={lat}&lon={lon}"
    for name, val in (("peak_kwp", peak_kwp), ("tilt", tilt),
                      ("azimuth", azimuth), ("loss", loss)):
        if val is not None:
            qs += f"&{name}={val}"
    return await _get(qs, ttl=1800)


async def weather_forecast(lat: float, lon: float) -> Optional[dict]:
    """AEMET daily forecast (7 days) → {forecast: [{date, temp_max, temp_min,
    description, precipitation_prob, municipality}]} (Panel environment)."""
    return await _get(f"/weather/forecast?lat={lat}&lon={lon}", ttl=1800)


async def weather_hourly(lat: float, lon: float, days: int = 3) -> Optional[dict]:
    """Open-Meteo HOURLY forecast for the coming `days` (max 7) → {hourly:
    [{ts, temp_c, feels_c, precip_prob_pct, cloud_pct, humidity_pct}]}.
    AEMET only offers daily at this horizon, so this is Open-Meteo-sourced."""
    return await _get(f"/weather/hourly-forecast?lat={lat}&lon={lon}&days={days}",
                      ttl=1800)


async def daylight(lat: float, lon: float) -> Optional[dict]:
    """Sunrise/sunset/daylight per day → {daily: [{date, sunrise, sunset,
    daylight_seconds}], hourly: [...]} (Panel solar card)."""
    return await _get(f"/weather/daylight?lat={lat}&lon={lon}", ttl=1800)


async def weather_observations(lat: float, lon: float) -> Optional[dict]:
    """Nearest AEMET station now → {data: {temperature, humidity,
    description, station, …}} (Panel 'ahora' row)."""
    return await _get(f"/weather/observations?lat={lat}&lon={lon}", ttl=900)


async def wind_forecast(lat: float, lon: float) -> Optional[dict]:
    """Hourly wind (Open-Meteo) → {hourly: [{ts, wind_speed_kmh,
    wind_gusts_kmh, wind_dir}]} (Panel wind/infiltration card)."""
    return await _get(f"/weather/wind?lat={lat}&lon={lon}&days=2", ttl=1800)


async def omie_day(day: str = "today") -> Optional[dict]:
    """Hourly OMIE day-ahead spot. Rows are quarter-hourly (15-min MTU) —
    callers aggregate per hour. 'tomorrow' goes through the range endpoint
    and returns None (400 upstream) until the auction is published."""
    if day == "today":
        return await _get("/prices/omie/today")
    from datetime import date, timedelta
    d = (date.today() + timedelta(days=1)).isoformat()
    return await _get(f"/prices/omie?start_date={d}&end_date={d}", ttl=900)


async def carbon_current() -> Optional[dict]:
    return await _get("/carbon/current", ttl=300)


async def price_forecast(lat: float, lon: float, days: int = 7,
                         access_tariff: str = "2.0TD") -> Optional[dict]:
    """WS-EXO-PRICE 7-day hourly electricity price forecast ->
    {status, model_status, method_by_day, hourly: [{date, hour, period_from,
    period_to, price_mean_eur_kwh, price_lo_eur_kwh, price_hi_eur_kwh,
    period, confidence, method, drivers}]}. D+1 is `method=official` when
    REE's D+1 demand+renewable forecasts are already published (there's a
    same-day publish-time gap — see api_exo's price_model.py — before which
    even "tomorrow" degrades to `method=proxy`/`climatology`); D+2-7 is
    `method=proxy` (weather-driven, wider band). None on failure — callers
    keep showing `price_status="forecast_pending"` for that day (S3)."""
    return await _get(
        f"/prices/forecast?days={days}&lat={lat}&lon={lon}&access_tariff={access_tariff}",
        ttl=3 * 3600,
    )


QUARTERS = ("00", "15", "30", "45")


def _fill_quarters(out: dict) -> dict:
    """Normalize a (date, "HH:MM")-keyed map to full quarter resolution.

    Upstream granularity varies (PVPC is hourly today; OMIE's apidatos
    fallback is hourly; native OMIE is quarter-hourly): any hour that has
    only its ":00" row gets the other three quarters filled from it — the
    hourly price IS the legally applicable price of each of its quarters.
    """
    for (ds, hhmm) in list(out.keys()):
        if hhmm[3:] != "00":
            continue
        for mm in QUARTERS[1:]:
            key = (ds, f"{hhmm[:2]}:{mm}")
            if key not in out:
                out[key] = out[(ds, hhmm)]
    return out


async def pvpc_qmap(start: str, end: str) -> dict:
    """(local_date, "HH:MM") → {price_eur_kwh, period} for the range.

    PVPC arrives hourly from api_exo today (ESIOS 1001) — expanded to
    quarters via _fill_quarters; if ESIOS ever ships native 15-min PVPC the
    same code passes it through untouched."""
    data = await pvpc_range(start, end)
    out: dict = {}
    for row in (data or {}).get("prices") or []:
        dl = str(row.get("datetime_local") or "")
        if len(dl) >= 16:
            out[(dl[:10], dl[11:16])] = {
                "price_eur_kwh": row.get("price_eur_kwh") or ((row.get("price_eur_mwh") or 0) / 1000.0),
                "period": row.get("period"),
            }
    return _fill_quarters(out)


async def omie_qmap(start: str, end: str) -> dict:
    """(local_date, "HH:MM") → eur_kwh for the range (indexed-contract cost).

    Native quarter-hourly (15-min MTU) rows pass through as-is; hourly
    responses (apidatos fallback) get quarter-filled. api_exo caps ranges at
    31 days, so long ranges are fetched in chunks. `status:"no_data"` days
    (e.g. tomorrow before the auction) simply leave gaps — never a 404.
    """
    import asyncio
    from datetime import date, timedelta
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    out: dict = {}
    chunks: list = []
    while d0 <= d1:
        chunk_end = min(d0 + timedelta(days=30), d1)
        chunks.append((d0, chunk_end))
        d0 = chunk_end + timedelta(days=1)
    coros = [
        _get(
            f"/prices/omie?start_date={c0.isoformat()}&end_date={c_end.isoformat()}",
            ttl=3600,
        )
        for (c0, c_end) in chunks
    ]
    results = await asyncio.gather(*coros)
    for data in results:
        for row in (data or {}).get("prices") or []:
            dl = str(row.get("datetime_local") or "")
            if len(dl) >= 16:
                kwh = row.get("price_eur_kwh") or ((row.get("price_eur_mwh") or 0) / 1000.0)
                out[(dl[:10], dl[11:16])] = float(kwh)
    return _fill_quarters(out)


async def tariff_schedule_week(day: str) -> Optional[dict]:
    """2.0TD band calendar, 7 days starting at `day` (holiday-aware) →
    {rows: [{datetime_local, weekday, hour, band}]} (168 rows)."""
    return await _get(
        f"/tariffs/schedule/week?start_date={day}&access_tariff=2.0TD", ttl=86400
    )


async def holidays(year: int, region: Optional[str] = None) -> Optional[dict]:
    """Spanish holidays for `year` (S4) → {holidays: [{date, name, is_national,
    region}]}. `is_national` drives TARIFF pricing (all-day P3, per CNMC
    2.0TD); the (regional) full list drives the BEHAVIOUR day-type regressor.
    7-day TTL — the calendar is static per year (matches api_exo's own cache)."""
    qs = f"/holidays?year={year}"
    if region:
        qs += f"&region={region}"
    return await _get(qs, ttl=7 * 24 * 3600)


async def weather_alerts(lat: float, lon: float) -> Optional[dict]:
    """Weather alerts + energy advisories (S5) → {alerts: [{severity,
    phenomenon, energy_advisory_es/en, ...}], count}. Heuristic today
    (Open-Meteo derived); same shape once WS-EXO-AEMET lands."""
    return await _get(f"/weather/alerts?lat={lat}&lon={lon}", ttl=1800)


async def climate_normals(lat: float, lon: float, days_before: int = 0,
                          days_after: int = 6) -> Optional[dict]:
    """30-yr day-of-year normals (S5 guardrail) → {normals: [{date, temp_mean,
    temp_max, temp_min}]}. 30-day TTL — normals barely move."""
    return await _get(
        f"/climate/normals?lat={lat}&lon={lon}&days_before={days_before}&days_after={days_after}",
        ttl=86400 * 30,
    )


# ── EcoFlow connector (read: service key; rule writes: admin key) ──

async def ecoflow_devices() -> Optional[dict]:
    """Registry + latest snapshots of the org's EcoFlow units (short cache —
    live SoC/W)."""
    return await _get("/ecoflow/devices", ttl=8)


async def ecoflow_rules() -> Optional[dict]:
    return await _get("/ecoflow/rules", ttl=8)


async def ecoflow_rule_log(hours: int = 48) -> Optional[dict]:
    return await _get(f"/ecoflow/rules/log?hours={hours}", ttl=15)


async def ecoflow_rule_update(rule_id: int, payload: dict) -> Optional[dict]:
    """PUT /ecoflow/rules/{id} — api_exo requires admin (EXO_ADMIN_API_KEY)."""
    url = f"{settings.EXO_BASE_URL.rstrip('/')}/api/v1/ecoflow/rules/{rule_id}"
    try:
        client = http_client.get_client()
        r = await client.put(url, json=payload, timeout=15.0,
                             headers={"X-API-Key": settings.EXO_ADMIN_API_KEY})
        if r.status_code != 200:
            logger.warning("api_exo PUT /ecoflow/rules/%s -> %s", rule_id, r.status_code)
            return None
        # rule state changed → drop cached reads
        _cache.pop("/ecoflow/rules", None)
        return r.json()
    except httpx.RequestError as e:
        logger.error("api_exo unreachable (ecoflow rule update): %s", e)
        return None
