"""Mi PLC (family remote access) — the CM4 local-webui proxied under
consum.pibico.es/plc/<webui_port>/.

Design: api_edge OWNS the tunnels (single-owner rule). It already runs the
`ssh -L 127.0.0.1:<webui_port>:127.0.0.1:8080` forward machinery and the
local-webui honors X-Forwarded-Prefix (built for api_edge's Webui tab).
api_consum adds the FAMILY layer on top:

  - tenancy: the requested webui_port must belong to the caller's own
    gateway (webui_port = ssh_port − 2000, deterministic formula);
  - tier: remote PLC access is the premium "acceso remoto seguro" → pro;
  - ensure/heal: server-side calls to api_edge's /tunnels/webui/ensure
    (admin service key from .env — never exposed to the browser).

nginx (consum.pibico.es.conf) terminates /plc/<port>/ with auth_request
against /api/v1/plc/auth and self-heals 502s via /api/v1/plc/ensure —
mirroring api_edge's nginx_tunnel_locations.conf pattern 1:1.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query

from app.api.v1.dependencies.rbac import ConsumContext, consum_context, require_tier
from app.api.v1.endpoints.consumption import _slugs
from app.services import consumption, edge_client

router = APIRouter(prefix="/plc", tags=["plc"])
logger = logging.getLogger("consum.plc")


async def _tcp_open(port: int, timeout: float = 2.0) -> bool:
    try:
        _, w = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port), timeout)
        w.close()
        return True
    except Exception:
        return False


async def _gateway(ctx: ConsumContext, customer: Optional[str]) -> Dict[str, Any]:
    """The caller's gateway device row (one gateway per household)."""
    slugs = await _slugs(ctx, customer, min_tier="pro")
    for d in await consumption.devices_for(slugs):
        if (d.get("device_type") or "gateway") == "gateway" and d.get("ssh_port"):
            return d
    raise HTTPException(404, detail="no gateway enrolled for this household")


def _webui_port(ssh_port: int) -> int:
    return ssh_port - 2000   # deterministic formula (api_edge core/ports.py)


async def _authorized_ports(ctx: ConsumContext) -> set[int]:
    """Every webui_port the caller may reach (their gateways)."""
    slugs = await _slugs(ctx, None, min_tier="pro")
    return {
        _webui_port(d["ssh_port"])
        for d in await consumption.devices_for(slugs)
        if d.get("ssh_port")
    }


@router.get("/session")
async def session(customer: Optional[str] = Query(None),
                  ctx: ConsumContext = Depends(require_tier("pro"))):
    """Resolve the caller's PLC and make sure the proxy path is live.
    Returns the /plc/<port>/ URL for the page's iframe, plus the full
    gateway list (one PLC per household — a user with several households
    picks theirs in the page selector; `?customer=` selects one)."""
    slugs = await _slugs(ctx, None, min_tier="pro")
    gateways = [
        {"id": g["id"], "hostname": g["hostname"], "customer": g["customer"],
         "webui_port": _webui_port(int(g["ssh_port"]))}
        for g in await consumption.devices_for(slugs)
        if g.get("ssh_port") and (g.get("device_type") or "gateway") == "gateway"
    ]
    d = await _gateway(ctx, customer)
    ssh_port = int(d["ssh_port"])
    port = _webui_port(ssh_port)
    online = await _tcp_open(ssh_port)
    if online:
        ok = await edge_client.webui_ensure(port)
        online = ok and await _tcp_open(port)
    return {"hostname": d["hostname"], "webui_port": port,
            "url": f"/plc/{port}/", "online": online,
            "last_seen": d.get("last_seen"),
            "gateways": gateways}


@router.get("/auth", include_in_schema=False)
async def auth(x_webui_port: int = Header(...),
               ctx: ConsumContext = Depends(consum_context)):
    """nginx auth_request for /plc/<port>/ — member+pro+TENANCY (the port
    must be one of the caller's own gateways; superadmin passes any)."""
    if ctx.tier not in ("pro", "enterprise"):
        raise HTTPException(403, detail="pro plan required")
    if not ctx.is_superadmin and x_webui_port not in await _authorized_ports(ctx):
        raise HTTPException(403, detail="not your gateway")
    return {"ok": True}


@router.get("/ensure", include_in_schema=False)
async def ensure(x_webui_port: int = Header(...),
                 ctx: ConsumContext = Depends(consum_context)):
    """nginx self-heal auth_request (error_page 502/504 on /plc/<port>/):
    same gate as /auth, then re-establish the forward via api_edge."""
    if ctx.tier not in ("pro", "enterprise"):
        raise HTTPException(403, detail="pro plan required")
    if not ctx.is_superadmin and x_webui_port not in await _authorized_ports(ctx):
        raise HTTPException(403, detail="not your gateway")
    if not await edge_client.webui_ensure(x_webui_port):
        raise HTTPException(503, detail="PLC offline")
    return {"ok": True}
