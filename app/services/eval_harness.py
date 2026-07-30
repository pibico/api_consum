"""eval_harness.py — accuracy evaluation harness (2026-07-30).

See plans/api_consum_plans/2026-07-30_eval_harness.md. Pure OBSERVABILITY:
periodically measures model accuracy from data ALREADY persisted elsewhere
(public.virtual_readings/sensor_hourly/sensor_data, consum.invoices,
exogenous.series) and writes a time series into consum.eval_metrics (mig
019) so a human can watch MAPE/WAPE trend and decide, with real numbers,
when to flip the dark flags (NILM_ENABLED, etc.). No forecasts are
re-persisted — every metric re-reads what other workers already wrote.

Read access: api_consum's TS role is SELECT-only on public.* (already used
by consumption.py/energy_series) and — checked live before writing this —
also holds USAGE+SELECT on exogenous.* (granted alongside api_exo when the
schema was created), so price_accuracy runs unconditionally; _exogenous_
grant_ok() still guards it defensively (schema/grant is owned by other
migrations, not this one — if it's ever revoked this must degrade to a
clean skip, never a 500).

Every metric function is: (a) pure w.r.t. its inputs (a time window), (b)
NEVER raises — a cold-start household/metric returns n=0 rows, logged, not
an exception — and (c) returns a list of dict rows ready for `_persist()`.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from psycopg.types.json import Jsonb

from app.core import db

logger = logging.getLogger("consum.eval_harness")

# ── Denominator floors — the spec's core anti-blowup rule: MAPE = |err|/|actual|
# explodes near actual≈0 (a household asleep at 3am draws a handful of watts).
# Flooring the denominator at a plausible "smallest sensible actual" keeps the
# percentage meaningful instead of reporting a std-alone 4000% night-time spike.
FLOOR_W = 30.0                  # whole-house / sub-circuit average power floor (W)
FLOOR_KWH = 0.05                # per-invoice energy floor (kWh)
FLOOR_EUR = 1.0                 # per-invoice currency floor (EUR)
FLOOR_PRICE_EUR_KWH = 0.01      # PVPC/OMIE price floor (EUR/kWh)

EXPECTED_LOOKBACK_DAYS = 60      # rolling window re-evaluated on every run
NILM_CHANNEL_PREFIX = "nilm_"   # must match api_edge's anomaly_service.nilm_channel()

# `sensor_hourly` (the permanent cagg) only covers data older than this many
# days — mirrors consumption.py's RAW_WINDOW_DAYS so a metric window that
# straddles the boundary reads each half from the right place. Imported (not
# redefined) to avoid the two constants drifting apart.
try:
    from app.services.consumption import RAW_WINDOW_DAYS
except Exception:  # noqa: BLE001 — never let an import wobble break eval_harness
    RAW_WINDOW_DAYS = 21


# ─────────────────────────── generic stats helper ───────────────────────────

def _stats(pairs: List[Tuple[float, float]], floor: float) -> Dict[str, Any]:
    """pairs = [(actual, predicted), ...] -> {mape, wape, bias, n} (%, %, same
    unit as the pairs, count). Denominator floored at `floor` per pair (MAPE)
    and in the WAPE aggregate. n=0 -> all-None, never a ZeroDivisionError."""
    n = len(pairs)
    if n == 0:
        return {"mape": None, "wape": None, "bias": None, "n": 0}
    abs_pct_errs: List[float] = []
    errs: List[float] = []
    sum_abs_actual = 0.0
    sum_abs_err = 0.0
    for actual, predicted in pairs:
        denom = max(abs(actual), floor)
        abs_pct_errs.append(abs(predicted - actual) / denom)
        errs.append(predicted - actual)
        sum_abs_actual += abs(actual)
        sum_abs_err += abs(predicted - actual)
    return {
        "mape": round(sum(abs_pct_errs) / n * 100.0, 2),
        "wape": round(sum_abs_err / max(sum_abs_actual, floor) * 100.0, 2),
        "bias": round(sum(errs) / n, 4),
        "n": n,
    }


# ───────────────────────── 1. expected_accuracy (3a) ─────────────────────────

async def _active_mains() -> List[Dict[str, Any]]:
    """Every active role='main' sensor + its household slug — the ONLY
    whole-house ground-truth source (never sum numeric channels, see
    CLAUDE.md / consumption.py's module docstring)."""
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute("""
                SELECT s.customer_id::text, c.slug, s.sensor_key, s.channel
                  FROM sensors s
                  JOIN customers c ON c.customer_id = s.customer_id
                 WHERE s.role = 'main' AND s.is_active
                 ORDER BY c.slug
            """)
            rows = await cur.fetchall()
    return [{"customer_id": r[0], "slug": r[1], "device": r[2], "channel": r[3]} for r in rows]


async def _actual_hourly_power(device: str, channel: str,
                               window_from: datetime, window_to: datetime) -> Dict[Any, float]:
    """Hourly avg-W {bucket: value} for one (device, channel). Hybrid read —
    same RAW_WINDOW_DAYS boundary as consumption.energy_series(): the part of
    the window older than the boundary reads the permanent `sensor_hourly`
    cagg, the recent part reads raw `sensor_data` (the cagg's materialization
    watermark hasn't caught up to it yet)."""
    boundary = datetime.now(timezone.utc) - timedelta(days=RAW_WINDOW_DAYS)
    out: Dict[Any, float] = {}
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            if window_from < boundary:
                await cur.execute("""
                    SELECT bucket, avg FROM sensor_hourly
                     WHERE variable = 'apower' AND device_id = %s AND channel = %s
                       AND bucket >= %s AND bucket < %s
                """, (device, channel, window_from, min(boundary, window_to)))
                for b, v in await cur.fetchall():
                    if v is not None:
                        out[b] = float(v)
            if window_to > boundary:
                lo = max(window_from, boundary)
                await cur.execute("""
                    SELECT time_bucket('1 hour', ts) AS b, AVG(value_num)
                      FROM sensor_data
                     WHERE variable = 'apower' AND device_id = %s AND channel = %s
                       AND ts >= %s AND ts < %s
                     GROUP BY b
                """, (device, channel, lo, window_to))
                for b, v in await cur.fetchall():
                    if v is not None:
                        out[b] = float(v)
    return out


async def _expected_hourly(customer_id: str, vkey: str, channel: str,
                           window_from: datetime, window_to: datetime) -> Dict[Any, float]:
    """Hourly {ts: value_num} for one virtual_readings series (always raw —
    it is its own small hypertable, one row per sensor per hour, no cagg)."""
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute("""
                SELECT ts, value_num FROM virtual_readings
                 WHERE variable = 'apower' AND channel = %s
                   AND customer_id = %s AND device_id = %s
                   AND ts >= %s AND ts < %s
            """, (channel, customer_id, vkey, window_from, window_to))
            rows = await cur.fetchall()
    return {ts: float(v) for ts, v in rows if v is not None}


async def expected_accuracy(window_from: datetime, window_to: datetime) -> List[Dict[str, Any]]:
    """virtual_readings 'expected_main' vs the real role='main' sensor's
    actual hourly avg power -> per-household + pooled fleet MAPE/WAPE/bias.
    This IS the honest "does the forecast hold up" proxy per the spec — no
    separate forecast table is persisted."""
    mains = await _active_mains()
    fleet_pairs: List[Tuple[float, float]] = []
    out: List[Dict[str, Any]] = []
    for m in mains:
        vkey = f"virtual_{m['device']}"
        actual = await _actual_hourly_power(m["device"], m["channel"], window_from, window_to)
        expected = await _expected_hourly(m["customer_id"], vkey, "expected_main", window_from, window_to)
        pairs = [(actual[t], expected[t]) for t in (actual.keys() & expected.keys())]
        stats = _stats(pairs, FLOOR_W)
        fleet_pairs.extend(pairs)
        out.append({"scope": "household", "slug": m["slug"], "horizon": None,
                    "unit": "pct_mape", **stats})
    out.append({"scope": "fleet", "slug": None, "horizon": None, "unit": "pct_mape",
               **_stats(fleet_pairs, FLOOR_W)})
    return out


# ─────────────────────────── 2. nilm_accuracy (N1) ───────────────────────────

async def _nilm_candidates() -> List[Dict[str, Any]]:
    """Households with BOTH an NILM archetype virtual channel AND a real
    (non-virtual, non-main) sub-circuit sensor to check it against.

    IMPORTANT (by design, not a bug): api_edge's nilm_worker only
    disaggregates households WITHOUT real per-circuit sub-metering
    (anomaly_service.list_main_sensors_without_subcircuits — see its
    docstring) precisely BECAUSE that's when an estimate is needed. A
    household that already has real sub-circuit meters doesn't get
    archetype channels written. So this overlap — archetype channel AND
    ground-truth sub-meter on the SAME household — is expected to be
    structurally empty most of the time; it only exists for a household
    temporarily carrying both (e.g. a calibration sub-meter, or the
    exclusion rule changing). The query is written so it picks up real
    data the moment it exists, with no code change. Match is best-effort:
    the archetype name (e.g. 'frigorifico') against the real sensor's
    `name` column."""
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute("""
                SELECT s.customer_id::text, c.slug, s.sensor_key, s.channel
                  FROM sensors s
                  JOIN customers c ON c.customer_id = s.customer_id
                 WHERE s.kind = 'virtual' AND s.channel LIKE %s
            """, (NILM_CHANNEL_PREFIX + "%",))
            archetypes = await cur.fetchall()
            if not archetypes:
                return []
            cids = list({r[0] for r in archetypes})
            await cur.execute("""
                SELECT customer_id::text, sensor_key, channel, COALESCE(name, '')
                  FROM sensors
                 WHERE customer_id::text = ANY(%s)
                   AND (kind IS NULL OR kind <> 'virtual')
                   AND (role IS NULL OR role <> 'main')
                   AND parent IS NOT NULL
            """, (cids,))
            subcircuits = await cur.fetchall()
    subs_by_cid: Dict[str, List[Tuple[str, str, str]]] = {}
    for cid, sk, ch, name in subcircuits:
        subs_by_cid.setdefault(cid, []).append((sk, ch, name))
    out: List[Dict[str, Any]] = []
    for cid, slug, vsk, vch in archetypes:
        archetype = vch[len(NILM_CHANNEL_PREFIX):]
        needle = archetype.split("_")[0].lower()
        match = next((s for s in subs_by_cid.get(cid, []) if needle in s[2].lower()), None)
        if match:
            out.append({"customer_id": cid, "slug": slug, "archetype": archetype,
                       "virtual_device": vsk, "real_device": match[0], "real_channel": match[1]})
    return out


async def nilm_accuracy(window_from: datetime, window_to: datetime) -> List[Dict[str, Any]]:
    """NILM archetype channels vs sub-metered actuals, per archetype. Clean
    cold-start (n=0, meta explains why) when no household currently has both
    sides of the comparison — see _nilm_candidates()."""
    candidates = await _nilm_candidates()
    if not candidates:
        return [{"scope": "fleet", "slug": None, "horizon": None, "unit": "pct_mape",
                 "mape": None, "wape": None, "bias": None, "n": 0,
                 "meta": {"cold_start": True, "reason": (
                     "no household currently has both an NILM archetype channel "
                     "and a matching real sub-circuit sensor — nilm_worker only "
                     "disaggregates households WITHOUT sub-metering, by design")}}]
    out: List[Dict[str, Any]] = []
    fleet_pairs_by_archetype: Dict[str, List[Tuple[float, float]]] = {}
    for c in candidates:
        actual = await _actual_hourly_power(c["real_device"], c["real_channel"], window_from, window_to)
        expected = await _expected_hourly(
            c["customer_id"], c["virtual_device"], f"{NILM_CHANNEL_PREFIX}{c['archetype']}",
            window_from, window_to)
        pairs = [(actual[t], expected[t]) for t in (actual.keys() & expected.keys())]
        stats = _stats(pairs, FLOOR_W)
        out.append({"scope": "household", "slug": c["slug"], "horizon": c["archetype"],
                    "unit": "pct_mape", **stats})
        fleet_pairs_by_archetype.setdefault(c["archetype"], []).extend(pairs)
    for archetype, pairs in fleet_pairs_by_archetype.items():
        out.append({"scope": "fleet", "slug": None, "horizon": archetype, "unit": "pct_mape",
                   **_stats(pairs, FLOOR_W)})
    return out


# ─────────────────────────── 3. bill_accuracy (2.5) ──────────────────────────

async def _closed_invoices() -> List[Dict[str, Any]]:
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute("""
                SELECT id, customer_id::text, period_start::text, period_end::text,
                       cups, energy_kwh, total_eur
                  FROM consum.invoices
                 WHERE status = 'closed'
                 ORDER BY period_start
            """)
            rows = await cur.fetchall()
    return [{"id": r[0], "customer_id": r[1], "period_start": r[2], "period_end": r[3],
            "cups": r[4], "energy_kwh": float(r[5]), "total_eur": float(r[6])} for r in rows]


async def bill_accuracy(window_from: datetime, window_to: datetime) -> List[Dict[str, Any]]:
    """Recompute bill_expectation.expected_bill for every past CLOSED invoice
    (exclude_invoice_id=itself avoids leakage — the same call the live
    bill-anomaly channel already makes in
    app/api/v1/endpoints/invoice.py::_compute_bill_analysis) vs that
    invoice's own actual kWh/EUR. Two sub-metrics (unit column):
    'pct_kwh' (energy MAPE) and 'pct_eur' (total-bill MAPE) — this is the
    €/kWh-style reliability check the spec asks for, expressed as two
    percentage series rather than a single blended number."""
    from app.services import advice_prefs, bill_expectation, consumption
    from app.services import supply_points as supply_points_svc

    invoices = await _closed_invoices()
    kwh_pairs: List[Tuple[float, float]] = []
    eur_pairs: List[Tuple[float, float]] = []
    per_household: Dict[str, Dict[str, List[Tuple[float, float]]]] = {}
    skipped = 0
    for inv in invoices:
        try:
            slug = await consumption.slug_for_customer(inv["customer_id"])
            slugs = [slug] if slug else []
            sp = await supply_points_svc.get_by_cups(inv["customer_id"], inv["cups"])
            equipment = (await consumption.comfort_flex_for(slugs, sp=sp)).get("equipment") if slugs else None
            region = (await advice_prefs.get(inv["customer_id"]) or {}).get("region")
            expected = await bill_expectation.expected_bill(
                inv["customer_id"], slugs, inv["cups"], inv["period_start"], inv["period_end"],
                supply_point_id=(sp or {}).get("id"), exclude_invoice_id=inv["id"],
                equipment=equipment, region=region)
        except Exception as exc:  # noqa: BLE001 — one bad invoice must never abort the batch
            logger.warning("bill_accuracy: invoice %s failed to recompute: %s", inv["id"], exc)
            skipped += 1
            continue
        if expected.get("status") != "ok" or expected.get("expected_kwh") is None:
            skipped += 1
            continue
        kp = (inv["energy_kwh"], float(expected["expected_kwh"]))
        kwh_pairs.append(kp)
        hh = per_household.setdefault(slug or "?", {"kwh": [], "eur": []})
        hh["kwh"].append(kp)
        if expected.get("expected_total_eur") is not None:
            ep = (inv["total_eur"], float(expected["expected_total_eur"]))
            eur_pairs.append(ep)
            hh["eur"].append(ep)

    out: List[Dict[str, Any]] = []
    for slug, d in per_household.items():
        out.append({"scope": "household", "slug": slug, "horizon": None, "unit": "pct_kwh",
                   **_stats(d["kwh"], FLOOR_KWH)})
        out.append({"scope": "household", "slug": slug, "horizon": None, "unit": "pct_eur",
                   **_stats(d["eur"], FLOOR_EUR)})
    out.append({"scope": "fleet", "slug": None, "horizon": None, "unit": "pct_kwh",
               "meta": {"skipped_invoices": skipped, "total_invoices": len(invoices)},
               **_stats(kwh_pairs, FLOOR_KWH)})
    out.append({"scope": "fleet", "slug": None, "horizon": None, "unit": "pct_eur",
               "meta": {"skipped_invoices": skipped, "total_invoices": len(invoices)},
               **_stats(eur_pairs, FLOOR_EUR)})
    return out


# ────────────────────── 4. price_accuracy (WS-EXO-PRICE) ─────────────────────

async def _exogenous_grant_ok() -> bool:
    """Defensive check — the exogenous schema/grant belongs to api_exo's
    migrations, not this one; if it's ever revoked this must degrade to a
    clean skip, never a 500."""
    try:
        async with db.raw_connection() as con:
            async with con.cursor() as cur:
                await cur.execute(
                    "SELECT has_table_privilege(current_user, 'exogenous.series', 'SELECT')")
                row = await cur.fetchone()
                return bool(row and row[0])
    except Exception as exc:  # noqa: BLE001
        logger.warning("price_accuracy: could not check exogenous grant: %s", exc)
        return False


async def price_accuracy(window_from: datetime, window_to: datetime) -> List[Dict[str, Any]]:
    """PRICE_FC (source='PRICE_FC', metric='eur_kwh', key='mean') vs realized
    PVPC (source='PVPC', metric='eur_kwh') for hours already in the past.

    KNOWN LIMITATION (documented, not a shortcut): `exogenous.series` is
    upserted keyed by (source, metric, zone, key, ts) — api_exo's daily
    WS-EXO-PRICE job REFRESHES the same target hour's row every day it's
    still in the forecast window, with no `produced_at`/snapshot-at-run-time
    column kept. By the time an hour becomes past, only the LAST write
    survives — there is no way to tell, from what's persisted today, whether
    that number was originally a D+1 or a D+6 forecast. True per-horizon
    (D+1 vs D+2..7) accuracy is therefore NOT reconstructable without api_exo
    persisting per-run snapshots, which is out of scope here (no cross-
    service changes). This computes one pooled accuracy instead and tags
    horizon='last_forecast_before_realization'."""
    if not await _exogenous_grant_ok():
        return [{"scope": "fleet", "slug": None, "horizon": None, "unit": "pct_mape",
                 "mape": None, "wape": None, "bias": None, "n": 0,
                 "meta": {"skipped": "no_select_grant_on_exogenous_series"}}]
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute("""
                SELECT ts, value FROM exogenous.series
                 WHERE source = 'PRICE_FC' AND metric = 'eur_kwh' AND key = 'mean'
                   AND ts >= %s AND ts < %s
            """, (window_from, window_to))
            fc_rows = await cur.fetchall()
            await cur.execute("""
                SELECT ts, value FROM exogenous.series
                 WHERE source = 'PVPC' AND metric = 'eur_kwh'
                   AND ts >= %s AND ts < %s
            """, (window_from, window_to))
            realized_rows = await cur.fetchall()
    forecast = {ts: float(v) for ts, v in fc_rows if v is not None}
    realized = {ts: float(v) for ts, v in realized_rows if v is not None}
    now = datetime.now(timezone.utc)
    pairs = [(realized[t], forecast[t]) for t in (forecast.keys() & realized.keys()) if t < now]
    stats = _stats(pairs, FLOOR_PRICE_EUR_KWH)
    return [{"scope": "fleet", "slug": None, "horizon": "last_forecast_before_realization",
             "unit": "pct_mape", **stats,
             "meta": {"note": "per-horizon D+1 vs D+2-7 split not reconstructable "
                              "from exogenous.series' current upsert-only persistence "
                              "— see price_accuracy() docstring"}}]


# ───────────────────────────── orchestrator ──────────────────────────────────

_METRIC_FNS = {
    "expected_accuracy": expected_accuracy,
    "nilm_accuracy": nilm_accuracy,
    "bill_accuracy": bill_accuracy,
    "price_accuracy": price_accuracy,
}


async def _persist(metric_key: str, rows: List[Dict[str, Any]],
                   window_from: datetime, window_to: datetime) -> int:
    if not rows:
        return 0
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            for r in rows:
                meta = dict(r.get("meta") or {})
                meta.setdefault("wape", r.get("wape"))
                meta.setdefault("bias", r.get("bias"))
                await cur.execute("""
                    INSERT INTO consum.eval_metrics
                        (metric_key, scope, slug, window_from, window_to,
                         horizon, value, unit, n, meta)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    metric_key, r.get("scope", "fleet"), r.get("slug"),
                    window_from, window_to, r.get("horizon"),
                    r.get("mape"), r.get("unit", "pct_mape"), r.get("n", 0),
                    Jsonb(meta),
                ))
    return len(rows)


async def run_all(window_days: Optional[int] = None) -> Dict[str, Any]:
    """Compute every metric and persist rows into consum.eval_metrics.
    Never raises — one metric's failure is logged and recorded as an n=0
    row, the batch continues. Returns a compact per-metric summary (rows
    written + the fleet-scope sample size) for the scheduler's log line and
    the manual-trigger endpoint's response."""
    now = datetime.now(timezone.utc)
    window_from = now - timedelta(days=window_days or EXPECTED_LOOKBACK_DAYS)
    summary: Dict[str, Any] = {}
    for key, fn in _METRIC_FNS.items():
        try:
            rows = await fn(window_from, now)
        except Exception as exc:  # noqa: BLE001 — one metric must never abort the run
            logger.error("eval_harness: metric %s raised: %s", key, exc)
            rows = [{"scope": "fleet", "slug": None, "horizon": None, "unit": "pct_mape",
                    "mape": None, "wape": None, "bias": None, "n": 0,
                    "meta": {"error": str(exc)}}]
        written = await _persist(key, rows, window_from, now)
        fleet_rows = [r for r in rows if r.get("scope") == "fleet"]
        summary[key] = {
            "rows_written": written,
            "fleet": [{"horizon": r.get("horizon"), "unit": r.get("unit"),
                      "value": r.get("mape"), "n": r.get("n")} for r in fleet_rows],
        }
    logger.info("eval_harness: run complete (window=%dd) — %s",
               window_days or EXPECTED_LOOKBACK_DAYS, summary)
    return {"computed_at": now.isoformat(), "window_from": window_from.isoformat(),
            "window_to": now.isoformat(), "metrics": summary}
