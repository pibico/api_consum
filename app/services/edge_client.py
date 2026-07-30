"""api_edge client — device metadata for the org's customers (F1).

Reads api_edge's JSON API with its service key. Only used for enrichment
(online state, device types) — raw telemetry comes straight from the shared
Timescale (services/consumption.py), NOT via api_edge.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from app.core import http_client
from app.core.config import settings

logger = logging.getLogger("consum.edge")

_cache: Dict[str, tuple[float, Any]] = {}
_TTL = 120


async def devices(customer_slug: str) -> Optional[List[dict]]:
    """GET api_edge /devices?customer=<slug> (service-key call)."""
    key = f"devices:{customer_slug}"
    now = time.time()
    hit = _cache.get(key)
    if hit and now < hit[0]:
        return hit[1]
    url = f"{settings.EDGE_BASE_URL.rstrip('/')}/api/v1/devices"
    try:
        client = http_client.get_client()
        r = await client.get(url, params={"customer": customer_slug},
                             headers={"X-API-Key": settings.EDGE_API_KEY}, timeout=10.0)
        if r.status_code != 200:
            logger.warning("api_edge /devices?customer=%s -> %s", customer_slug, r.status_code)
            return None
        data = (r.json() or {}).get("data") or []
        _cache[key] = (now + _TTL, data)
        return data
    except httpx.RequestError as e:
        logger.error("api_edge unreachable: %s", e)
        return None


async def topology(customer_slug: str,
                   gateway: Optional[str] = None) -> Optional[dict]:
    """GET api_edge /topology?customer=<slug>[&gateway=<hostname>] — the CM4
    wiring tree with live watts; `gateway` picks ONE PLC when the customer
    has several (multi-CUPS F3). Returns the payload (may be
    {status:"offline"}) or None if api_edge is unreachable."""
    key = f"topology:{customer_slug}:{gateway or ''}"
    now = time.time()
    hit = _cache.get(key)
    if hit and now < hit[0]:
        return hit[1]
    url = f"{settings.EDGE_BASE_URL.rstrip('/')}/api/v1/topology"
    params = {"customer": customer_slug}
    if gateway:
        params["gateway"] = gateway
    try:
        client = http_client.get_client()
        r = await client.get(url, params=params,
                             headers={"X-API-Key": settings.EDGE_API_KEY}, timeout=12.0)
        if r.status_code != 200:
            logger.warning("api_edge /topology?customer=%s -> %s", customer_slug, r.status_code)
            return None
        data = r.json()
        _cache[key] = (now + 4, data)   # live watts → short cache (5s Sankey refresh)
        return data
    except httpx.RequestError as e:
        logger.error("api_edge /topology unreachable: %s", e)
        return None


async def webui_ensure(webui_port: int) -> bool:
    """Ask api_edge (tunnel owner) to (re)establish the ssh -L forward onto
    the CM4 local-webui. Admin service key — server-side only."""
    url = f"{settings.EDGE_BASE_URL.rstrip('/')}/api/v1/tunnels/webui/ensure"
    try:
        client = http_client.get_client()
        r = await client.get(url, headers={"X-API-Key": settings.EDGE_API_KEY,
                                           "X-Webui-Port": str(webui_port)},
                             timeout=20.0)
        return r.status_code == 200
    except httpx.RequestError as e:
        logger.error("api_edge webui/ensure unreachable: %s", e)
        return False


async def anomalies(customer_slug: str, since_days: int = 14,
                    limit: int = 50) -> Optional[List[dict]]:
    """GET api_edge `/api/v1/anomalies?slug=` (Phase 2 E4 — statistical
    anomaly write-path, mig 024 anomaly_events). Same EDGE_API_KEY already
    used for /devices and /topology: api_edge's `org_scope()` dependency
    treats a valid non-admin service key as unrestricted-read (rbac.py), so
    no new credential or api_edge-side change is needed. Returns the raw row
    list (full explainability `meta` per row) or None if api_edge is
    unreachable/errors — the caller (anomaly_poller) treats None as
    "keep the last good cache", not as "no anomalies"."""
    from datetime import datetime, timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
    url = f"{settings.EDGE_BASE_URL.rstrip('/')}/api/v1/anomalies"
    try:
        client = http_client.get_client()
        r = await client.get(url, params={"slug": customer_slug, "from": since, "limit": limit},
                             headers={"X-API-Key": settings.EDGE_API_KEY}, timeout=10.0)
        if r.status_code != 200:
            logger.warning("api_edge /anomalies?slug=%s -> %s", customer_slug, r.status_code)
            return None
        return (r.json() or {}).get("data") or []
    except httpx.RequestError as e:
        logger.error("api_edge /anomalies unreachable: %s", e)
        return None


async def shelly_switch(customer_slug: str, sensor_key: str, on: bool) -> Optional[dict]:
    """POST api_edge /mqtt/sensors/{key}/switch — Switch.Set over MQTT.
    Admin service key, server-side only; tenancy re-checked by api_edge."""
    url = f"{settings.EDGE_BASE_URL.rstrip('/')}/api/v1/mqtt/sensors/{sensor_key}/switch"
    try:
        client = http_client.get_client()
        r = await client.post(url, params={"customer": customer_slug},
                              json={"on": bool(on)},
                              headers={"X-API-Key": settings.EDGE_API_KEY},
                              timeout=10.0)
        if r.status_code != 200:
            logger.warning("api_edge switch %s -> %s: %s", sensor_key,
                           r.status_code, r.text[:200])
            return None
        return r.json()
    except httpx.RequestError as e:
        logger.error("api_edge switch unreachable: %s", e)
        return None
