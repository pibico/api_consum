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


async def solar_forecast(lat: float, lon: float) -> Optional[dict]:
    return await _get(f"/weather/solar-forecast?lat={lat}&lon={lon}", ttl=1800)


async def weather_forecast(lat: float, lon: float) -> Optional[dict]:
    """AEMET daily forecast (7 days) → {forecast: [{date, temp_max, temp_min,
    description, precipitation_prob, municipality}]} (Panel environment)."""
    return await _get(f"/weather/forecast?lat={lat}&lon={lon}", ttl=1800)


async def weather_observations(lat: float, lon: float) -> Optional[dict]:
    """Nearest AEMET station now → {data: {temperature, humidity,
    description, station, …}} (Panel 'ahora' row)."""
    return await _get(f"/weather/observations?lat={lat}&lon={lon}", ttl=900)


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


async def pvpc_map(start: str, end: str) -> dict:
    """(local_date, hour) → {price_eur_kwh, period} for the range (F2 cost)."""
    data = await pvpc_range(start, end)
    out = {}
    for row in (data or {}).get("prices") or []:
        dt_local = str(row.get("datetime_local") or "")[:10]
        h = row.get("hour")
        if dt_local and h is not None:
            out[(dt_local, int(h))] = {
                "price_eur_kwh": row.get("price_eur_kwh") or ((row.get("price_eur_mwh") or 0) / 1000.0),
                "period": row.get("period"),
            }
    return out
