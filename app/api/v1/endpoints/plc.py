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
from pydantic import BaseModel, Field

from app.api.v1.dependencies.rbac import ConsumContext, consum_context, require_tier
from app.api.v1.endpoints.consumption import _slugs, _sp
from app.services import consumption, edge_client, exo_client

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


async def _gateway(ctx: ConsumContext, customer: Optional[str],
                   hostname: Optional[str] = None) -> Dict[str, Any]:
    """The caller's gateway device row. `hostname` (F3) elige UN PLC cuando el
    hogar tiene varios — con el mismo slug, ?customer= no distingue."""
    slugs = await _slugs(ctx, customer, min_tier="pro")
    for d in await consumption.devices_for(slugs):
        if (d.get("device_type") or "gateway") == "gateway" and d.get("ssh_port"):
            if hostname and d.get("hostname") != hostname:
                continue
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
                  gateway: Optional[str] = Query(None, description="hostname del PLC (F3)"),
                  supply: Optional[int] = Query(None),
                  ctx: ConsumContext = Depends(require_tier("pro"))):
    """Resolve the caller's PLC and make sure the proxy path is live.
    Returns the /plc/<port>/ URL for the page's iframe, plus the full
    gateway list (one PLC per household — a user with several households
    picks theirs in the page selector; `?customer=` selects one)."""
    slugs = await _slugs(ctx, None, min_tier="pro")
    if supply and not gateway:
        # Cascada F3: el punto de suministro global decide el PLC.
        sp = await _sp(slugs, supply)
        gateway = (sp or {}).get("gateway_hostname")
    gateways = [
        {"id": g["id"], "hostname": g["hostname"], "customer": g["customer"],
         "webui_port": _webui_port(int(g["ssh_port"]))}
        for g in await consumption.devices_for(slugs)
        if g.get("ssh_port") and (g.get("device_type") or "gateway") == "gateway"
    ]
    d = await _gateway(ctx, customer, hostname=gateway)
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


# ── EcoFlow (baterías del hogar via api_exo) + conmutación de sus Shellys ──
#
# api_exo's EcoFlow registry is platform-global (one vendor account), so the
# household gate is the RULES layer: a rule's params.customer names the owning
# slug — only households named there (or superadmin) see the card.

_ECOFLOW_PARAM_KEYS = {"soc_min", "soc_max", "price_quantile", "min_toggle_minutes"}


async def _ecoflow_rules_for(ctx: ConsumContext, slugs: list[str]) -> list[dict]:
    data = await exo_client.ecoflow_rules() or {}
    rules = data.get("rules") or []
    if ctx.is_superadmin:
        return rules
    return [r for r in rules if (r.get("params") or {}).get("customer") in slugs]


@router.get("/ecoflow")
async def ecoflow(gateway: Optional[str] = Query(None),
                  supply: Optional[int] = Query(None),
                  ctx: ConsumContext = Depends(require_tier("pro"))):
    """EcoFlow card payload: devices + rules + recent decisions. Empty when
    the caller's households own no EcoFlow rule (feature not contracted).
    Scoped to ONE PLC: the units live at a site — a rule tagged with
    params.gateway_hostname only shows on that PLC (rules without the tag
    show everywhere, legacy)."""
    slugs = await _slugs(ctx, None, min_tier="pro")
    if supply and not gateway:
        sp = await _sp(slugs, supply)
        gateway = (sp or {}).get("gateway_hostname")
    rules = await _ecoflow_rules_for(ctx, slugs)
    if gateway:
        rules = [r for r in rules
                 if (r.get("params") or {}).get("gateway_hostname") in (None, gateway)]
    if not rules:
        return {"devices": [], "rules": [], "log": []}
    devs = await exo_client.ecoflow_devices() or {}
    log = await exo_client.ecoflow_rule_log(48) or {}
    return {
        "connected": devs.get("connected"),
        "devices": devs.get("devices") or [],
        "rules": rules,
        "log": (log.get("log") or [])[:12],
    }


class SwitchBody(BaseModel):
    on: bool


@router.post("/shelly/{sensor_key}/switch")
async def shelly_switch(sensor_key: str, body: SwitchBody,
                        ctx: ConsumContext = Depends(require_tier("pro"))):
    """Switch one of the household's own smart plugs (EcoFlow charge lines).
    Tenancy: the sensor must be a registered plug of one of the caller's
    slugs; api_edge re-validates and publishes the MQTT Switch.Set."""
    from app.core import db

    slugs = await _slugs(ctx, None, min_tier="pro")
    ids = await consumption._slugs_to_ids(slugs)
    if not ids:
        raise HTTPException(404, detail="sin hogares")
    async with db.raw_connection() as con:
        cur = await con.execute(
            "SELECT customer_id::text FROM sensors "
            "WHERE sensor_key = %s AND customer_id = ANY(%s::uuid[]) "
            "AND (kind = 'plug' OR sensor_type = 'switch') LIMIT 1",
            (sensor_key, list(ids.values())),
        )
        row = await cur.fetchone()
    if not row:
        raise HTTPException(404, detail="ese enchufe no pertenece a tu hogar")
    slug = next(s for s, i in ids.items() if i == row[0])
    res = await edge_client.shelly_switch(slug, sensor_key, body.on)
    if not res:
        raise HTTPException(502, detail="no se pudo conmutar (api_edge)")
    return res


class EcoflowRuleBody(BaseModel):
    enabled: bool | None = None
    mode: str | None = Field(None, pattern="^(auto|manual)$")
    params: dict | None = None  # whitelist: soc_min/soc_max/price_quantile/min_toggle_minutes


@router.put("/ecoflow/rule/{rule_id}")
async def ecoflow_rule(rule_id: int, body: EcoflowRuleBody,
                       ctx: ConsumContext = Depends(require_tier("pro"))):
    """Auto/manual + thresholds of the household's charge rule (proxied to
    api_exo with the admin key — target/customer fields are NOT editable
    from here, only the whitelisted tuning knobs)."""
    slugs = await _slugs(ctx, None, min_tier="pro")
    rules = await _ecoflow_rules_for(ctx, slugs)
    if not any(r.get("id") == rule_id for r in rules):
        raise HTTPException(404, detail="regla no encontrada")
    payload: dict = {}
    if body.enabled is not None:
        payload["enabled"] = body.enabled
    if body.mode:
        payload["mode"] = body.mode
    if body.params:
        clean = {k: v for k, v in body.params.items() if k in _ECOFLOW_PARAM_KEYS}
        if clean:
            payload["params"] = clean
    if not payload:
        raise HTTPException(400, detail="nada que actualizar")
    res = await exo_client.ecoflow_rule_update(rule_id, payload)
    if not res:
        raise HTTPException(502, detail="api_exo no pudo actualizar la regla")
    return res
