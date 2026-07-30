"""bill_reconcile.py — Phase 2.5 B5: PLC-metered vs invoiced kWh per tariff
period (P1/P2/P3), for households that HAVE a PLC at this CUPS.

Ground truth for THIS check is the METER (unlike the bill-anomaly channel,
whose ground truth is the bill) — it answers a different question:
"does what the PLC measured for this exact billing period match what the
bill charged?" (estimated reads, meter gaps, billing errors).

Skips CLEANLY ("not_available") when there is no registered `role='main'`
sensor for this CUPS (`sp is None` — no PLC / no topology) or when the PLC
simply has no data for the period (gap in the timeseries, e.g. the PLC was
offline for the whole billing cycle) — NEVER falls back to summing numeric
channels (that double-counts subcircuits, see consumption.py's module
docstring) and never raises just because a household happens to have no
PLC (the majority of the bill-anomaly channel's households, by design).

Timestamp convention: `consumption.energy_series()`'s hourly buckets are
consumed EXACTLY like `invoices._settle()` already does (r["ts"][:10] /
r["ts"][11:13] as local date/hour, no further tz conversion) — matching
`pricing.period_map()`'s (local_date, "HH:MM") keys.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Sequence

from app.core.config import settings
from app.services import consumption, pricing

logger = logging.getLogger("consum.bill_reconcile")

_BANDS = ("P1", "P2", "P3")


async def reconcile(slugs: Sequence[str], sp: Optional[Dict[str, Any]],
                    period_start: str, period_end: str,
                    invoiced_bands: Dict[str, Optional[float]],
                    invoiced_total_kwh: Optional[float],
                    is_estimated: bool = False,
                    pct_tol: Optional[float] = None) -> Dict[str, Any]:
    """{status, reason, by_period:[{period, metered_kwh, invoiced_kwh,
    delta_kwh, pct}], total:{metered_kwh, invoiced_kwh, delta_kwh, pct},
    verdict: 'ok'|'estimated_read'|'meter_gap'|None}.

    `status` is 'ok' (a reconciliation was computed — `verdict` is then
    meaningful) or 'not_available' (`reason`: 'no_plc'|'no_meter_data'|
    'no_invoiced_kwh') — the UI renders "no disponible" for the latter,
    never an error."""
    if sp is None:
        return {"status": "not_available", "reason": "no_plc",
                "by_period": [], "total": None, "verdict": None}

    rows = await consumption.energy_series(slugs, period_start, period_end,
                                           bucket="hour", sp=sp)
    if not rows:
        return {"status": "not_available", "reason": "no_meter_data",
                "by_period": [], "total": None, "verdict": None}

    invoiced_total = invoiced_total_kwh
    if invoiced_total is None:
        known = [v for v in invoiced_bands.values() if v is not None]
        invoiced_total = sum(known) if known else None
    if invoiced_total is None:
        return {"status": "not_available", "reason": "no_invoiced_kwh",
                "by_period": [], "total": None, "verdict": None}

    pmap = await pricing.period_map(period_start, period_end)
    metered = {p: 0.0 for p in _BANDS}
    for r in rows:
        ts = r["ts"]
        d, h = ts[:10], ts[11:13]
        if d < period_start or d > period_end:
            continue
        band = pmap.get((d, f"{h}:00"))
        if band in metered:
            metered[band] += r["kwh"]

    metered_total = sum(metered.values())
    by_period = []
    for p in _BANDS:
        inv_kwh = invoiced_bands.get(p)
        met_kwh = round(metered[p], 2)
        delta = round(met_kwh - inv_kwh, 2) if inv_kwh is not None else None
        pct = round(delta / inv_kwh * 100, 1) if (inv_kwh not in (None, 0) and delta is not None) else None
        by_period.append({"period": p, "metered_kwh": met_kwh, "invoiced_kwh": inv_kwh,
                          "delta_kwh": delta, "pct": pct})

    total_delta = round(metered_total - invoiced_total, 2)
    total_pct = round(total_delta / invoiced_total * 100, 1) if invoiced_total else None
    tol = pct_tol if pct_tol is not None else settings.RECON_PCT_TOL

    if is_estimated:
        verdict = "estimated_read"
    elif total_pct is None:
        verdict = None
    elif abs(total_pct) <= tol:
        verdict = "ok"
    else:
        verdict = "meter_gap"

    return {"status": "ok", "reason": None, "by_period": by_period,
            "total": {"metered_kwh": round(metered_total, 2),
                      "invoiced_kwh": round(invoiced_total, 2),
                      "delta_kwh": total_delta, "pct": total_pct},
            "verdict": verdict}
