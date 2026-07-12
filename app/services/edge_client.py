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


async def topology(customer_slug: str) -> Optional[dict]:
    """GET api_edge /topology?customer=<slug> — the CM4 wiring tree with live
    watts. Returns the payload (may be {status:"offline"}) or None if api_edge
    is unreachable. Short TTL since the watts are live."""
    key = f"topology:{customer_slug}"
    now = time.time()
    hit = _cache.get(key)
    if hit and now < hit[0]:
        return hit[1]
    url = f"{settings.EDGE_BASE_URL.rstrip('/')}/api/v1/topology"
    try:
        client = http_client.get_client()
        r = await client.get(url, params={"customer": customer_slug},
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
