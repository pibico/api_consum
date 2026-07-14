"""Puntos de suministro (multi-CUPS F3) — una instalación real por cada
medidor de cabecera (`sensors.role='main'`): su PLC, su CUPS/contrato y su
localización.

Derivación perezosa: `ensure_for_slugs()` upserta un punto por cada main del
registro `sensors` (nube, sincronizado desde el appliances.yaml del PLC) —
nombre = nombre del sensor main, gateway/localización desde `devices` del
gateway (api_edge). Idempotente y barato; se invoca desde
`/consumption/context`.

`subtree()` expande un punto a sus (device_id, channel): la raíz main + los
descendientes por `sensors.parent` ("<sensor_key>/<channel>") — es la unidad
de filtrado de TODOS los lectores de consumo con `?supply=`.

Tenancy: como el resto del servicio, cada función exige customer_ids YA
autorizados por rbac (app-level, sin RLS por columnstore).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core import db

logger = logging.getLogger("consum.supply_points")


def _row_to_dict(r: Sequence[Any]) -> Dict[str, Any]:
    return {
        "id": r[0], "customer_id": str(r[1]), "name": r[2], "cups": r[3],
        "main_sensor_key": r[4], "main_channel": r[5],
        "gateway_hostname": r[6], "location": r[7],
        "latitude": float(r[8]) if r[8] is not None else None,
        "longitude": float(r[9]) if r[9] is not None else None,
    }


_SELECT = """SELECT id, customer_id, name, cups, main_sensor_key, main_channel,
                    gateway_hostname, location, latitude, longitude
               FROM consum.supply_points"""


async def ensure_for_slugs(customer_ids: Sequence[str]) -> List[Dict[str, Any]]:
    """Upsert de un punto por cada `role='main'` del registro y devolución de
    todos los puntos de esos customers. El nombre/CUPS puestos por el usuario
    se conservan (COALESCE); gateway/localización se refrescan del registro."""
    if not customer_ids:
        return []
    cids = [str(c) for c in customer_ids]
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            # mains registrados + su gateway (para hostname/location/coords)
            await cur.execute(
                """SELECT s.customer_id::text, s.sensor_key, s.channel,
                          COALESCE(NULLIF(s.name,''), s.sensor_key),
                          d.hostname, d.location, d.latitude, d.longitude
                     FROM sensors s
                LEFT JOIN devices d ON d.id = s.gateway_id
                    WHERE s.customer_id::text = ANY(%s) AND s.role = 'main'""",
                (cids,),
            )
            mains = await cur.fetchall()
            for m in mains:
                cid, skey, ch, name, gw_host, gw_loc, lat, lon = m
                await cur.execute(
                    """INSERT INTO consum.supply_points
                         (customer_id, name, main_sensor_key, main_channel,
                          gateway_hostname, location, latitude, longitude)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT (customer_id, main_sensor_key, main_channel)
                       DO UPDATE SET
                         gateway_hostname = COALESCE(EXCLUDED.gateway_hostname,
                                                     consum.supply_points.gateway_hostname),
                         location  = COALESCE(consum.supply_points.location,
                                              EXCLUDED.location),
                         latitude  = COALESCE(consum.supply_points.latitude,
                                              EXCLUDED.latitude),
                         longitude = COALESCE(consum.supply_points.longitude,
                                              EXCLUDED.longitude),
                         updated_at = now()""",
                    (cid, name, skey, ch, gw_host, gw_loc, lat, lon),
                )
            await con.commit()
            await cur.execute(
                _SELECT + " WHERE customer_id::text = ANY(%s) ORDER BY id", (cids,))
            return [_row_to_dict(r) for r in await cur.fetchall()]


async def get(sp_id: int, customer_ids: Sequence[str]) -> Optional[Dict[str, Any]]:
    """Un punto POR ID, solo si pertenece a un customer autorizado."""
    if not sp_id or not customer_ids:
        return None
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                _SELECT + " WHERE id = %s AND customer_id::text = ANY(%s)",
                (sp_id, [str(c) for c in customer_ids]),
            )
            r = await cur.fetchone()
    return _row_to_dict(r) if r else None


async def subtree(sp: Dict[str, Any]) -> List[Tuple[str, str]]:
    """(device_id, channel) del sub-árbol del punto: la raíz main + TODOS los
    descendientes vía `sensors.parent`. BFS en memoria sobre el registro del
    customer (decenas de filas) — sin recursión SQL."""
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                """SELECT sensor_key, channel, parent FROM sensors
                    WHERE customer_id::text = %s AND is_active""",
                (sp["customer_id"],),
            )
            rows = await cur.fetchall()
    children: Dict[str, List[Tuple[str, str]]] = {}
    for skey, ch, parent in rows:
        if parent:
            children.setdefault(parent, []).append((skey, ch))
    out: List[Tuple[str, str]] = []
    seen: set = set()
    stack = [(sp["main_sensor_key"], sp["main_channel"])]
    while stack:
        skey, ch = stack.pop()
        key = f"{skey}/{ch}"
        if key in seen:
            continue
        seen.add(key)
        out.append((skey, ch))
        stack.extend(children.get(key, []))
    return out


async def update_fields(sp_id: int, customer_ids: Sequence[str],
                        name: Optional[str] = None,
                        location: Optional[str] = None,
                        cups: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Edición del usuario (nombre/etiqueta de localización/CUPS)."""
    sp = await get(sp_id, customer_ids)
    if not sp:
        return None
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                """UPDATE consum.supply_points
                      SET name = COALESCE(NULLIF(%s,''), name),
                          location = COALESCE(%s, location),
                          cups = COALESCE(NULLIF(%s,''), cups),
                          updated_at = now()
                    WHERE id = %s""",
                (name, location, cups, sp_id),
            )
        await con.commit()
    return await get(sp_id, customer_ids)


async def bind_contract_cups(customer_id: str, cups: Optional[str]
                             ) -> Optional[int]:
    """Auto-vinculación por CUPS (decisión 13-07): devuelve el supply_point_id
    para un contrato con ese CUPS, o None (contrato sin punto = legado).
      1. Un punto del customer ya tiene ese cups → ese.
      2. CUPS nuevo y hay EXACTAMENTE UN punto sin cups → lo adopta.
      3. Ambiguo/sin match → None (el usuario vincula a mano)."""
    if not cups:
        return None
    cups = cups.strip().upper()
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                """SELECT id, cups FROM consum.supply_points
                    WHERE customer_id::text = %s ORDER BY id""",
                (str(customer_id),),
            )
            pts = await cur.fetchall()
            match = [p for p in pts if (p[1] or "").strip().upper() == cups]
            if match:
                return match[0][0]
            empty = [p for p in pts if not p[1]]
            if len(empty) == 1:
                await cur.execute(
                    "UPDATE consum.supply_points SET cups=%s, updated_at=now() WHERE id=%s",
                    (cups, empty[0][0]),
                )
                await con.commit()
                logger.info("supply point %s adopta CUPS %s…", empty[0][0], cups[:8])
                return empty[0][0]
    return None
