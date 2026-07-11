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
