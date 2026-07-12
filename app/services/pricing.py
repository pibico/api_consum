"""Contract-aware pricing — the single costing dispatcher (entidad contrato).

QUARTER-HOURLY inside, HOURLY settlement outside (2026-07 refactor):
Spain's wholesale market is 15-min (96 prices/day) but DOMESTIC settlement
stays hourly — retailers bill the hourly meter curve at the plain mean of the
hour's four quarter prices. Accordingly:
  - price_map() keys by (local_date, "HH:MM") — the analytic truth the Panel
    charts (indexed contracts get the exact quarter OMIE price, no averaging).
  - hourly_rollup() collapses that to (local_date, hour) with the plain
    quarter mean — THE settlement path: bills, estimates, CSV, savings all
    consume it, matching what the retailer actually invoices.

Every €-from-kWh path goes through price_map_for_slugs()/hourly_rollup();
households without a contract (or multi-household aggregates) fall back to
PVPC, byte-identical to the pre-contract behavior. Sources: api_exo PVPC
(all-in regulated price with P1/P2/P3 band, hourly today — quarter-filled),
api_exo OMIE (raw spot, native 15-min) and the /tariffs 2.0TD holiday-aware
calendar.

No caching here: costing recomputes per request (exo_client already caches the
market data, which is contract-independent), so a contract edit takes effect
on the next fetch — no invalidation problem by construction.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.services import contracts as contracts_svc
from app.services import exo_client

logger = logging.getLogger("consum.pricing")

FALLBACK_SOURCE = "pvpc"
QUARTERS = exo_client.QUARTERS  # ("00", "15", "30", "45")


def _band(hour: int, weekend: bool) -> str:
    # 2.0TD calendar (last-resort fallback — misses holidays, log when used):
    # weekends/holidays are all-P3; weekdays P1 10-14/18-22,
    # P2 8-10/14-18/22-24, P3 0-8.
    if weekend:
        return "P3"
    if 10 <= hour < 14 or 18 <= hour < 22:
        return "P1"
    if 8 <= hour < 10 or 14 <= hour < 18 or 22 <= hour < 24:
        return "P2"
    return "P3"


async def period_map(start: str, end: str,
                     pvpc: Optional[dict] = None) -> Dict[Tuple[str, str], str]:
    """(local_date, "HH:MM") → P1|P2|P3 for the range. Sources in order:
    api_exo /tariffs/schedule/week (holiday-aware, hourly → ×4 quarters) →
    same-quarter PVPC band → static _band(). Bands are an hourly calendar, so
    all four quarters of an hour share its band."""
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    out: Dict[Tuple[str, str], str] = {}
    d = d0
    while d <= d1:
        wk = await exo_client.tariff_schedule_week(d.isoformat())
        for row in (wk or {}).get("rows") or []:
            dl = str(row.get("datetime_local") or "")
            band = row.get("band")
            if len(dl) >= 13 and band:
                for mm in QUARTERS:
                    out[(dl[:10], f"{dl[11:13]}:{mm}")] = band
        d += timedelta(days=7)
    # Fallbacks for any quarter the schedule didn't cover
    if pvpc is None:
        pvpc = await exo_client.pvpc_qmap(start, end)
    d = d0
    fell_back = False
    while d <= d1:
        ds = d.isoformat()
        weekend = d.weekday() >= 5
        for h in range(24):
            for mm in QUARTERS:
                key = (ds, f"{h:02d}:{mm}")
                if key not in out:
                    row = (pvpc or {}).get(key)
                    out[key] = (row or {}).get("period") or _band(h, weekend)
                    fell_back = True
        d += timedelta(days=1)
    if fell_back:
        logger.warning("period_map fell back to PVPC/static bands for %s..%s", start, end)
    return out


def _fixed_price(contract: Dict[str, Any], period: str) -> Optional[float]:
    # Single-price contracts leave P2/P3 NULL → everything bills at P1.
    p = {
        "P1": contract.get("energy_p1_eur_kwh"),
        "P2": contract.get("energy_p2_eur_kwh"),
        "P3": contract.get("energy_p3_eur_kwh"),
    }.get(period)
    return p if p is not None else contract.get("energy_p1_eur_kwh")


def _indexed_price(contract: Dict[str, Any], omie_kwh: float, period: str) -> float:
    passthru = contract.get(f"passthru_{period.lower()}_eur_kwh") or 0.0
    return omie_kwh + (contract.get("margin_eur_kwh") or 0.0) + passthru


def _covering(contracts: List[Dict[str, Any]], ds: str) -> Optional[Dict[str, Any]]:
    for c in contracts:
        if c["start_date"] <= ds and (c["end_date"] is None or c["end_date"] >= ds):
            return c
    return None


async def price_map(contract_list: List[Dict[str, Any]], start: str, end: str) -> dict:
    """(local_date, "HH:MM") → {price_eur_kwh, period, source} for the range.

    QUARTER resolution — the analytic map (indexed contracts price each
    15-min OMIE slot exactly). Each local date is owned by the contract
    covering it (mid-range contract switches supported); uncovered days —
    and pvpc-type contracts — cost at PVPC. For SETTLEMENT (what the retailer
    bills: hourly meter curve × hourly quarter-mean) collapse with
    hourly_rollup().
    """
    pvpc = await exo_client.pvpc_qmap(start, end)
    if not contract_list:
        return {k: {**v, "source": FALLBACK_SOURCE} for k, v in pvpc.items()}

    types_needed = set()
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    d = d0
    while d <= d1:
        c = _covering(contract_list, d.isoformat())
        types_needed.add(c["contract_type"] if c else "pvpc")
        d += timedelta(days=1)

    omie = await exo_client.omie_qmap(start, end) if "indexed" in types_needed else {}
    periods = (await period_map(start, end, pvpc=pvpc)
               if types_needed & {"fixed", "indexed"} else {})

    out: dict = {}
    d = d0
    while d <= d1:
        ds = d.isoformat()
        c = _covering(contract_list, ds)
        ctype = c["contract_type"] if c else "pvpc"
        weekend = d.weekday() >= 5
        for h in range(24):
            for mm in QUARTERS:
                key = (ds, f"{h:02d}:{mm}")
                if ctype == "fixed":
                    period = periods.get(key) or _band(h, weekend)
                    price = _fixed_price(c, period)
                    if price is not None:
                        out[key] = {"price_eur_kwh": price, "period": period,
                                    "source": "fixed"}
                elif ctype == "indexed":
                    if key in omie:
                        period = periods.get(key) or _band(h, weekend)
                        out[key] = {"price_eur_kwh": _indexed_price(c, omie[key], period),
                                    "period": period, "source": "indexed"}
                else:  # pvpc-type contract or uncovered day
                    if key in pvpc:
                        out[key] = {**pvpc[key], "source": FALLBACK_SOURCE}
        d += timedelta(days=1)
    return out


def hourly_rollup(pmap: dict) -> dict:
    """Quarter map → (local_date, hour) → {price_eur_kwh, period, source}.

    Plain (unweighted) mean of the hour's quarter prices — exactly how
    domestic settlement works (the retailer applies the hourly quarter-mean
    to the hourly meter curve). Period/source are constant within the hour
    by construction (band calendar and contract ownership are per-hour/day).
    """
    acc: Dict[Tuple[str, int], Dict[str, Any]] = {}
    for (ds, hhmm), row in pmap.items():
        if row.get("price_eur_kwh") is None:
            continue
        e = acc.setdefault((ds, int(hhmm[:2])),
                           {"sum": 0.0, "n": 0, "period": row.get("period"),
                            "source": row.get("source")})
        e["sum"] += row["price_eur_kwh"]
        e["n"] += 1
    # round(…, 8) so the mean of 4 identical quarter prices reproduces the
    # hourly price EXACTLY (no float-ULP drift in CSV/JSON output)
    return {k: {"price_eur_kwh": round(v["sum"] / v["n"], 8), "period": v["period"],
                "source": v["source"]}
            for k, v in acc.items() if v["n"]}


async def price_map_for_slugs(slugs: Sequence[str], start: str, end: str
                              ) -> Tuple[dict, str]:
    """(QUARTER price map, source label). Source label is the ACTIVE
    contract's type today-or-range-end (what the UI badges); 'pvpc' when no
    contract applies."""
    _, contract_list = await contracts_svc.resolve_for_slugs(slugs, start, end)
    pmap = await price_map(contract_list, start, end)
    label = FALLBACK_SOURCE
    if contract_list:
        c = _covering(contract_list, end) or contract_list[-1]
        label = c["contract_type"]
    return pmap, label


async def hourly_price_map_for_slugs(slugs: Sequence[str], start: str, end: str
                                     ) -> Tuple[dict, str]:
    """(HOURLY settlement price map, source label) — the drop-in for every
    billing-faithful consumer (month/bands/forecast/CSV/savings)."""
    pmap, label = await price_map_for_slugs(slugs, start, end)
    return hourly_rollup(pmap), label


async def price_now(contract: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """{price_eur_kwh, period, source} for the current local QUARTER (Panel
    KPI — indexed homes see the live 15-min slot price)."""
    now = datetime.now()
    today, h = now.date().isoformat(), now.hour
    q = f"{h:02d}:{(now.minute // 15) * 15:02d}"
    if contract is None or contract["contract_type"] == "pvpc":
        pvpc = await exo_client.pvpc_day("today")
        row = next((p for p in (pvpc or {}).get("prices") or []
                    if p.get("hour") == h), None)
        if not row:
            return None
        return {"price_eur_kwh": row.get("price_eur_kwh")
                or ((row.get("price_eur_mwh") or 0) / 1000.0),
                "period": row.get("period"), "source": FALLBACK_SOURCE}
    periods = await period_map(today, today)
    period = periods.get((today, q)) or _band(h, now.weekday() >= 5)
    if contract["contract_type"] == "fixed":
        price = _fixed_price(contract, period)
        if price is None:
            return None
        return {"price_eur_kwh": price, "period": period, "source": "fixed"}
    omie = await exo_client.omie_qmap(today, today)
    if (today, q) not in omie:
        return None
    return {"price_eur_kwh": _indexed_price(contract, omie[(today, q)], period),
            "period": period, "source": "indexed"}


async def bill_breakdown(contract: Dict[str, Any],
                         kwh_by_hour: Dict[Tuple[str, int], float],
                         start: str, end: str) -> Dict[str, Any]:
    """Full 2.0TD bill estimate for [start, end] under one contract.

    Energy by period + power term (kW × €/kW/día × days) + prorated fixed
    charges + IEE on (energy+power, meter rental excluded — legal base) +
    VAT on everything.

    HOURLY settlement (kwh_by_hour keyed (date, hour)): domestic retailers
    bill the hourly meter curve at the hourly quarter-mean price — the
    quarter-exact analytic view is the Panel's job, not the bill's.
    """
    pmap = hourly_rollup(await price_map([contract], start, end))
    energy = {p: {"kwh": 0.0, "eur": 0.0} for p in ("P1", "P2", "P3")}
    uncosted_kwh = 0.0
    for key, kwh in kwh_by_hour.items():
        row = pmap.get(key)
        if not row or row.get("price_eur_kwh") is None:
            uncosted_kwh += kwh
            continue
        p = row.get("period") if row.get("period") in energy else "P3"
        energy[p]["kwh"] += kwh
        energy[p]["eur"] += kwh * row["price_eur_kwh"]
    energy_eur = sum(v["eur"] for v in energy.values())
    energy_kwh = sum(v["kwh"] for v in energy.values())

    days = (date.fromisoformat(end) - date.fromisoformat(start)).days + 1
    power_eur = sum(
        (contract.get(f"power_{p}_kw") or 0.0) * (contract.get(f"power_{p}_eur_kw_day") or 0.0)
        for p in ("p1", "p2")
    ) * days
    fixed_eur = ((contract.get("meter_rental_eur_month") or 0.0)
                 + (contract.get("other_fixed_eur_month") or 0.0)) * 12 / 365 * days
    iee_eur = (energy_eur + power_eur) * (contract.get("electricity_tax_pct") or 0.0) / 100
    subtotal = energy_eur + power_eur + iee_eur + fixed_eur
    vat_eur = subtotal * (contract.get("vat_pct") or 0.0) / 100
    total = subtotal + vat_eur

    return {
        "start": start, "end": end, "days": days,
        "contract_id": contract["id"], "contract_type": contract["contract_type"],
        "energy": {p: {"kwh": round(v["kwh"], 2), "eur": round(v["eur"], 2)}
                   for p, v in energy.items()},
        "energy_kwh": round(energy_kwh, 2),
        "energy_eur": round(energy_eur, 2),
        "uncosted_kwh": round(uncosted_kwh, 2),
        "power_eur": round(power_eur, 2),
        "fixed_eur": round(fixed_eur, 2),
        "iee_eur": round(iee_eur, 2),
        "vat_eur": round(vat_eur, 2),
        "total_eur": round(total, 2),
        "avg_eur_kwh": round(total / energy_kwh, 5) if energy_kwh else None,
    }
