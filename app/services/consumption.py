"""Consumption aggregates over the shared Timescale (pibiconnect_ts) — F1.

Data model (verified live 2026-07-11):
  sensor_data(ts, customer_id uuid, device_id text=hostname, channel text,
              variable text, value_num float)
  - `apower`         → instantaneous power, W (per device+channel)
  - `apower_energy`  → CUMULATIVE energy counter, Wh (per device+channel)
  - Household energy meter (Shelly EM) publishes NUMERIC channels ('0','1');
    smart plugs publish 'switch:0'. Summing every device double-counts (the
    EM already measures the whole house), so "whole house" totals use ONLY
    numeric-channel devices; per-device series are exposed unfiltered.

kWh per bucket = GREATEST(max(counter)-min(counter), 0)/1000 per
device+channel (monotonic counter; the GREATEST clamp absorbs resets),
summed across channels. Tenancy is APP-LEVEL (TS columnstore ⊥ RLS): every
query REQUIRES customer slugs already authorized by rbac.ConsumContext.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

from fastapi import HTTPException

from app.core import db

logger = logging.getLogger("consum.consumption")

# Whole-house filter: EM channels are plain integers ('0','1','2'); plug/PM
# channels look like 'switch:0'. See module docstring.
_HOUSE_CHANNEL_SQL = "channel ~ '^[0-9]+$'"


async def _slugs_to_ids(slugs: Sequence[str]) -> Dict[str, str]:
    """slug → customer_id (uuid str) for the authorized slugs."""
    if not slugs:
        return {}
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                "SELECT slug, customer_id FROM customers WHERE slug = ANY(%s)",
                (list(slugs),),
            )
            return {r[0]: str(r[1]) for r in await cur.fetchall()}


async def all_slugs() -> List[str]:
    """Every customer slug (superadmin fleet view)."""
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute("SELECT slug FROM customers ORDER BY slug")
            return [r[0] for r in await cur.fetchall()]


async def location_for(slugs: Sequence[str]) -> Optional[Dict[str, Any]]:
    """The household's coords (customers.latitude/longitude, mig 020 —
    api_edge owns the column; set at onboarding). First slug with coords
    wins (one home per org in the family model); None → caller defaults."""
    if not slugs:
        return None
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                """
                SELECT latitude, longitude, municipality FROM customers
                WHERE slug = ANY(%s) AND latitude IS NOT NULL AND longitude IS NOT NULL
                ORDER BY slug LIMIT 1
                """,
                (list(slugs),),
            )
            row = await cur.fetchone()
    if not row:
        return None
    return {"lat": float(row[0]), "lon": float(row[1]), "municipality": row[2]}


async def solar_config_for(slugs: Sequence[str]) -> Optional[Dict[str, Any]]:
    """The household's rooftop-PV config (consum.solar_config, mig 003) — the
    installed kWp (+ optional tilt/azimuth/loss) that scales the Panel solar
    estimate. First slug with a row wins (one home per org). None → the caller
    lets api_exo apply its 3 kWp default."""
    ids = await _slugs_to_ids(slugs)
    if not ids:
        return None
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                """SELECT peak_kwp, tilt, azimuth, loss FROM consum.solar_config
                    WHERE customer_id = ANY(%s) ORDER BY customer_id LIMIT 1""",
                (list(ids.values()),),
            )
            row = await cur.fetchone()
    if not row:
        return None
    return {"peak_kwp": float(row[0]),
            "tilt": float(row[1]) if row[1] is not None else None,
            "azimuth": float(row[2]) if row[2] is not None else None,
            "loss": float(row[3]) if row[3] is not None else None}


async def set_solar_config(slug: str, peak_kwp: float,
                           tilt: Optional[float] = None,
                           azimuth: Optional[float] = None,
                           loss: Optional[float] = None,
                           updated_by: Optional[str] = None) -> Dict[str, Any]:
    """Upsert the household's rooftop-PV config. Only the fields provided are
    written; tilt/azimuth/loss default to NULL (→ api_exo defaults)."""
    ids = await _slugs_to_ids([slug])
    cid = ids.get(slug)
    if not cid:
        raise HTTPException(404, detail=f"Hogar desconocido: {slug}")
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                """INSERT INTO consum.solar_config
                       (customer_id, peak_kwp, tilt, azimuth, loss, updated_by)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (customer_id) DO UPDATE SET
                       peak_kwp = EXCLUDED.peak_kwp,
                       tilt = EXCLUDED.tilt, azimuth = EXCLUDED.azimuth,
                       loss = EXCLUDED.loss, updated_by = EXCLUDED.updated_by,
                       updated_at = now()""",
                (cid, peak_kwp, tilt, azimuth, loss, updated_by),
            )
    return {"peak_kwp": peak_kwp, "tilt": tilt, "azimuth": azimuth, "loss": loss}


async def devices_for(slugs: Sequence[str]) -> List[Dict[str, Any]]:
    """Devices (id, hostname, type, customer slug) for the authorized slugs."""
    ids = await _slugs_to_ids(slugs)
    if not ids:
        return []
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                """
                SELECT d.id, d.hostname, d.device_type, c.slug, d.last_seen, d.ssh_port
                FROM devices d JOIN customers c ON c.customer_id = d.customer_id
                WHERE c.slug = ANY(%s)
                ORDER BY c.slug, d.hostname
                """,
                (list(ids.keys()),),
            )
            return [
                {"id": r[0], "hostname": r[1], "device_type": r[2],
                 "customer": r[3],
                 "last_seen": r[4].isoformat() if r[4] else None,
                 "ssh_port": r[5]}
                for r in await cur.fetchall()
            ]


async def sensor_devices(slugs: Sequence[str]) -> List[Dict[str, Any]]:
    """Energy-capable sensors from api_edge's `sensors` REGISTRY (mig 017) —
    the authoritative sensor→gateway assignment (devices = the gateways; the
    MQTT stream itself carries no gateway ref). Friendly `name` curated in
    the api_edge console; falls back to the sensor_key."""
    ids = await _slugs_to_ids(slugs)
    if not ids:
        return []
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                """
                SELECT sensor_key, MAX(name) AS name
                FROM sensors
                WHERE customer_id::text = ANY(%s) AND is_active
                  AND ('apower' = ANY(variables) OR 'apower_energy' = ANY(variables))
                GROUP BY sensor_key ORDER BY sensor_key
                """,
                (list(ids.values()),),
            )
            return [{"id": r[0], "name": r[1] or r[0]}
                    for r in await cur.fetchall()]


async def current_power(slugs: Sequence[str]) -> Dict[str, Any]:
    """Latest instantaneous power per device (+ whole-house total from the
    EM numeric channels only). Looks at the last 10 minutes."""
    ids = await _slugs_to_ids(slugs)
    if not ids:
        return {"total_w": 0, "devices": []}
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                """
                SELECT DISTINCT ON (device_id, channel)
                       device_id, channel, value_num, ts
                FROM sensor_data
                WHERE customer_id::text = ANY(%s)
                  AND variable = 'apower'
                  AND ts > now() - interval '10 minutes'
                ORDER BY device_id, channel, ts DESC
                """,
                (list(ids.values()),),
            )
            rows = await cur.fetchall()
    devices: Dict[str, Dict[str, Any]] = {}
    total = 0.0
    for device_id, channel, value, ts in rows:
        d = devices.setdefault(device_id, {"device": device_id, "power_w": 0.0,
                                           "channels": {}, "ts": ts.isoformat()})
        d["channels"][channel] = value
        d["power_w"] += value or 0.0
        if channel and channel.isdigit():
            total += value or 0.0
    # No EM present → fall back to the sum of everything (plug-only homes)
    if total == 0.0 and rows:
        total = sum(d["power_w"] for d in devices.values())
    return {"total_w": round(total, 1), "devices": sorted(devices.values(), key=lambda d: -d["power_w"])}


async def energy_series(
    slugs: Sequence[str],
    start: str,
    end: str,
    bucket: str = "hour",
    device: Optional[str] = None,
    house_only: bool = True,
) -> List[Dict[str, Any]]:
    """kWh per time bucket. `device` filters one hostname (then house_only is
    ignored — you asked for that device); otherwise whole-house (EM channels).
    start/end: ISO dates (YYYY-MM-DD) or timestamps, inclusive start,
    exclusive end+1d when a bare date is given."""
    ids = await _slugs_to_ids(slugs)
    if not ids:
        return []
    trunc = {"quarter": "15 minutes", "hour": "1 hour", "day": "1 day"}.get(bucket, "1 hour")
    hours_per_bucket = {"15 minutes": 0.25, "1 hour": 1.0, "1 day": 24.0}[trunc]
    where = ["customer_id::text = ANY(%s)",
             "ts >= %s::timestamptz", "ts < (%s::timestamptz + interval '1 day')"]
    params: List[Any] = [list(ids.values()), start, end]
    if device:
        where.append("device_id = %s")
        params.append(device)
    elif house_only:
        where.append(_HOUSE_CHANNEL_SQL)
    cond = " AND ".join(where)
    # Counter-based kWh (devices WITH apower_energy — the EM meter) ...
    sql_counter = f"""
        SELECT time_bucket(%s::interval, ts) AS bucket, device_id, channel,
               GREATEST(MAX(value_num) - MIN(value_num), 0) / 1000.0 AS kwh
        FROM sensor_data
        WHERE variable = 'apower_energy' AND {cond}
        GROUP BY bucket, device_id, channel
    """
    # ... and power integration for plugs that ONLY publish instantaneous
    # `apower` (kWh ≈ avg W × bucket hours / 1000; Shellies report ~1/min,
    # so the approximation is tight while the device is online).
    sql_power = f"""
        SELECT time_bucket(%s::interval, ts) AS bucket, device_id, channel,
               AVG(value_num) * {hours_per_bucket} / 1000.0 AS kwh
        FROM sensor_data
        WHERE variable = 'apower' AND {cond}
        GROUP BY bucket, device_id, channel
    """
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(sql_counter, [trunc] + params)
            counter_rows = await cur.fetchall()
            await cur.execute(sql_power, [trunc] + params)
            power_rows = await cur.fetchall()
    # Counter wins per (device, channel); integration only fills the gaps.
    countered = {(r[1], r[2]) for r in counter_rows}
    buckets: Dict[Any, float] = {}
    for b, _, _, kwh in counter_rows:
        buckets[b] = buckets.get(b, 0.0) + float(kwh or 0)
    for b, dev, ch, kwh in power_rows:
        if (dev, ch) not in countered:
            buckets[b] = buckets.get(b, 0.0) + float(kwh or 0)
    return [
        {"ts": b.isoformat(), "kwh": round(k, 3)}
        for b, k in sorted(buckets.items())
    ]


async def topology_from_sensors(slugs: Sequence[str]) -> Dict[str, Any]:
    """Offline fallback for the wiring Sankey: rebuild the tree from the
    persisted `sensors` topology (name/parent/role/kind, synced from the PLC)
    + live power from Timescale — same shape as the CM4's diagram(), so the
    frontend renders it identically when the PLC is unreachable."""
    ids = await _slugs_to_ids(slugs)
    if not ids:
        return {"status": "offline", "reason": "no_household"}
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                """SELECT sensor_key, channel, name, parent, role, kind
                     FROM sensors
                    WHERE customer_id::text = ANY(%s)
                      AND (role = 'main' OR parent IS NOT NULL)""",
                (list(ids.values()),),
            )
            rows = await cur.fetchall()
    if not rows:
        return {"status": "offline", "reason": "no_topology"}
    power = await current_power(slugs)
    pmap: Dict[tuple, float] = {}
    for d in power.get("devices") or []:
        for ch, w in (d.get("channels") or {}).items():
            pmap[(d["device"], ch)] = w
    nodes: Dict[str, Dict[str, Any]] = {}
    for sk, ch, name, parent, role, kind in rows:
        nid = f"{sk}/{ch}"
        nodes[nid] = {"key": nid, "name": name or nid, "kind": kind or "",
                      "role": role or "", "online": True,
                      "power": pmap.get((sk, ch)), "children": []}
    root = None
    for sk, ch, name, parent, role, kind in rows:
        nid = f"{sk}/{ch}"
        if role == "main":
            root = nodes[nid]
    for sk, ch, name, parent, role, kind in rows:
        nid = f"{sk}/{ch}"
        if role == "main":
            continue
        if parent and parent in nodes:
            nodes[parent]["children"].append(nodes[nid])
        elif root is not None:
            root["children"].append(nodes[nid])
    return {"status": "offline_fallback", "source": "persisted",
            "root": root, "unplaced": [], "net": None, "ts": 0}


async def power_peak_hourly(slugs: Sequence[str], date: str,
                            device: Optional[str] = None) -> List[Dict[str, Any]]:
    """Peak instantaneous power (W) reached in each hour of one local day.
    Whole-house = MAX over the EM mains channels (the mains dominates, so its
    peak IS the household demand — no channel summing to avoid double-counting
    a sub-circuit); a `device` narrows to that plug. Feeds the 'demand per
    hour' card (relevant to the 2.0TD power term / maxímetro)."""
    ids = await _slugs_to_ids(slugs)
    if not ids:
        return []
    where = ["variable = 'apower'", "customer_id::text = ANY(%s)",
             "ts >= %s::timestamptz", "ts < (%s::timestamptz + interval '1 day')"]
    params: List[Any] = [list(ids.values()), date, date]
    if device:
        where.append("device_id = %s")
        params.append(device)
    else:
        where.append(_HOUSE_CHANNEL_SQL)
    sql = f"""
        SELECT time_bucket('1 hour', ts) AS h, MAX(value_num) AS peak_w
        FROM sensor_data WHERE {" AND ".join(where)}
        GROUP BY h ORDER BY h
    """
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(sql, params)
            rows = await cur.fetchall()
    return [{"hour": r[0].hour, "peak_w": round(float(r[1] or 0), 1)}
            for r in rows if r[0].date().isoformat() == date]


async def summary(slugs: Sequence[str]) -> Dict[str, Any]:
    """kWh today / last 7 days / last 30 days (whole house)."""
    ids = await _slugs_to_ids(slugs)
    if not ids:
        return {"today_kwh": 0, "week_kwh": 0, "month_kwh": 0}
    sql = f"""
        SELECT SUM(kwh) FROM (
            SELECT device_id, channel,
                   GREATEST(MAX(value_num) - MIN(value_num), 0) / 1000.0 AS kwh
            FROM sensor_data
            WHERE customer_id::text = ANY(%s) AND variable = 'apower_energy'
              AND {_HOUSE_CHANNEL_SQL}
              AND ts >= %s::timestamptz
            GROUP BY device_id, channel
        ) sub
    """
    out: Dict[str, Any] = {}
    windows = {
        "today_kwh": "date_trunc('day', now())",
        "week_kwh": "now() - interval '7 days'",
        "month_kwh": "now() - interval '30 days'",
    }
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            for key, since_expr in windows.items():
                await cur.execute(f"SELECT {since_expr}")
                since = (await cur.fetchone())[0]
                await cur.execute(sql, (list(ids.values()), since))
                row = await cur.fetchone()
                out[key] = round(float(row[0] or 0), 2)
    return out
