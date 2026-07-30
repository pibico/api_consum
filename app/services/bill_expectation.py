"""bill_expectation.py — Phase 2.5 B1: the weather/price/tariff-adjusted
expected bill for one invoice's billing period, at one CUPS.

Ground truth here is the MEMBER'S OWN invoice history for that installation
(not the meter) — this is what makes the channel work for invoice-only
households with no PLC (spec §"works for invoice-only households"). The
model reuses Phase 1's exact machinery unmodified:
  - `oe3._fit_variance_safe` / `oe3._day_type_series` — the SAME
    variance-safe, equipment-gated degree-day OLS fit `forecast_explained`
    uses, just fit at INVOICE granularity (one observation per historical
    bill: its own average HDD/CDD and weekend/holiday fraction over the
    period, vs. its kWh/day) instead of daily granularity.
  - `shap_attr.attribute_forecast` — the exact linear Shapley decomposition
    (Phase 1 S2), applied to the invoice-level fit.
Cross-module reuse of leading-underscore helpers mirrors an existing
pattern in this codebase (`api/v1/endpoints/invoice.py` already imports
`_slugs`/`_sp` from `endpoints/consumption.py`).

GOTCHAS handled here (spec 2026-07-28 Phase 2.5):
  - €/day + kWh/day normalization EVERYWHERE — billing periods vary
    28-35 days (even bi-monthly) — comparing raw totals is the #1
    false-positive source. Every historical sample and the target period
    are reduced to a PER-DAY rate before any comparison; totals are only
    reconstructed at the very end (× the period's OWN day count).
  - Sparse baseline: <MIN_HIST_FOR_ANY prior invoices → status
    'insufficient_data' (never fires — mirrors the existing simple
    `_compute_anomaly` guard); <MIN_HIST_FOR_REGRESSION → still compares,
    but as a flat historical mean (no weather/calendar split) and
    `sparse=True` (caller widens its anomaly thresholds / suppresses the
    last-year ref accordingly).
  - Breakdown JSONB shape drift: `components_for()` is the ONE adapter that
    flattens both a GENERATED invoice's first-class columns and an
    UPLOADED bill's `ai_amounts` extraction into the same shape — a field
    that isn't knowable for a given invoice is None, NEVER coerced to 0
    (an uploaded bill's power_eur COLUMN is always 0 — see store_uploaded;
    treating that as a real 0 would manufacture a spurious "potencia
    dropped to zero" driver on every uploaded invoice).
  - potencia (power) vs energía: modeled as its OWN component, never
    folded into the energy/weather regression.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.services import consumption, exo_client, pricing
from app.services import invoices as invoices_svc
from app.services import shap_attr
from app.services.oe3 import _day_type_series, _fit_variance_safe  # noqa: F401 (reuse, see docstring)

logger = logging.getLogger("consum.bill_expectation")

MIN_HIST_FOR_ANY = 2          # spec: "never fire on a single-invoice history"
MIN_HIST_FOR_REGRESSION = 6   # below this: flat historical mean, no weather/calendar split
MIN_HIST_FOR_LAST_YEAR = 1    # a same-period-last-year ref needs at least one candidate

_FEATURE_LABELS = {"hdd": "Frío (grados-día)", "cdd": "Calor (grados-día)",
                  "calendar": "Fin de semana / festivos"}
_FEATURE_UNITS = {"hdd": "kWh/día", "cdd": "kWh/día", "calendar": "kWh/día"}

_ESTIMATED_RE = re.compile(
    r"factura\s+estimad|lectura\s+estimad|consumo\s+estimad|estimated\s+(read|bill)",
    re.IGNORECASE)


# ── Uniform per-invoice component adapter (the "breakdown JSONB shape drift"
# gotcha) — the ONE place that knows how to read both a GENERATED and an
# UPLOADED invoice into the same {energy_eur, power_eur, fixed_eur, tax_eur,
# energy_kwh, days, total_eur} shape. A None field means "not knowable for
# THIS invoice" — averaging code must skip Nones, never treat as zero. ─────

def _days(inv: Dict[str, Any]) -> Optional[int]:
    try:
        d = (date.fromisoformat(inv["period_end"]) - date.fromisoformat(inv["period_start"])).days + 1
    except (TypeError, ValueError, KeyError):
        return None
    return d if d > 0 else None


def components_for(inv: Dict[str, Any]) -> Dict[str, Any]:
    """Uniform bill components for ONE invoice row (from invoices.get() /
    history_for_cups() — needs `breakdown`). See module docstring."""
    days = _days(inv)
    if inv.get("origin") == "uploaded":
        bd = inv.get("breakdown") or {}
        am = bd.get("ai_amounts")
        if am:
            energy_eur = round((am.get("energia_eur") or 0) + (am.get("peajes_eur") or 0), 2)
            power_eur = am.get("potencia_eur")
            fixed_eur = round((am.get("alquiler_eur") or 0) + (am.get("bono_social_eur") or 0), 2)
            tax_eur = am.get("impuestos_eur")
            kwh = ((am.get("kwh_horas_caras") or 0) + (am.get("kwh_horas_normales") or 0)
                  + (am.get("kwh_horas_baratas") or 0)) or None
        else:
            # No AI extraction yet (most historical uploads — the amounts
            # panel/B4 backfill hasn't run for them): power/fixed/tax are
            # genuinely unknowable, but energy_eur is NOT — a bill is always
            # total = energy + power + fixed + tax, so with the other three
            # unknown (treated as 0 below) the WHOLE total is the best
            # available proxy for the energy share. This is coarser than a
            # real split (it folds any power/fixed/tax drift into the
            # energy/price driver too) but lets the weather/calendar/
            # consumption/price decomposition run on real invoices instead
            # of degrading to a single opaque "no breakdown" driver — see
            # bill_attr.py's docstring for the exact-identity guarantee
            # that holds regardless.
            power_eur = fixed_eur = tax_eur = None
            energy_eur = inv.get("total_eur")
            kwh = ((inv.get("energy_p1_kwh") or 0) + (inv.get("energy_p2_kwh") or 0)
                  + (inv.get("energy_p3_kwh") or 0)) or None
    else:
        energy_eur = inv.get("energy_eur")
        power_eur = inv.get("power_eur")
        fixed_eur = inv.get("fixed_eur")
        tax_eur = round((inv.get("iee_eur") or 0) + (inv.get("vat_eur") or 0), 2)
        kwh = inv.get("energy_kwh") or None
    return {"days": days, "energy_eur": energy_eur, "power_eur": power_eur,
            "fixed_eur": fixed_eur, "tax_eur": tax_eur, "energy_kwh": kwh,
            "total_eur": inv.get("total_eur"), "origin": inv.get("origin"),
            "period_start": inv.get("period_start"), "period_end": inv.get("period_end")}


def is_estimated_read(inv: Dict[str, Any]) -> bool:
    """Lectura estimada detection (spec gotcha #1) — an explicit parsed flag,
    a Spanish/English "estimated" phrase in the stored bill text, or the
    numeric heuristic (round total kWh with NO per-band split at all — a
    real meter read almost always yields a non-round P1/P2/P3 split).
    Labelled, never treated as a fault — callers suppress/soften the
    anomaly verdict for a period flagged this way."""
    bd = inv.get("breakdown") or {}
    ex = bd.get("extracted") or {}
    if isinstance(ex.get("estimated"), bool) and ex["estimated"]:
        return True
    md = bd.get("markdown") or ""
    if md and _ESTIMATED_RE.search(md):
        return True
    kwh = inv.get("energy_kwh")
    bands_present = any(inv.get(k) for k in ("energy_p1_kwh", "energy_p2_kwh", "energy_p3_kwh"))
    if kwh and not bands_present:
        try:
            k = float(kwh)
            if k > 0 and k == round(k) and k % 5 == 0:
                return True
        except (TypeError, ValueError):
            pass
    return False


async def _period_features(lat: float, lon: float, start: str, end: str,
                           region: Optional[str]) -> Optional[Dict[str, float]]:
    """Mean HDD/CDD + weekend/holiday FRACTION over [start, end] — the
    invoice-level regressors (one number per historical bill, unlike
    forecast_explained's one-row-per-day). None when api_exo has no degree-
    day data for the range (cold start / outage) — caller degrades to the
    flat historical mean."""
    dd = await exo_client.degree_days(lat, lon, start, end)
    rows = (dd or {}).get("days") or []
    if not rows:
        return None
    hdd_mean = sum(r.get("hdd") or 0.0 for r in rows) / len(rows)
    cdd_mean = sum(r.get("cdd") or 0.0 for r in rows) / len(rows)
    dates = [r["date"] for r in rows if r.get("date")]
    day_types = await _day_type_series(dates, region)
    cal_mean = (sum(day_types.values()) / len(day_types)) if day_types else 0.0
    return {"hdd": hdd_mean, "cdd": cdd_mean, "calendar": cal_mean}


async def _hist_energy_samples(hist: List[Dict[str, Any]], lat: float, lon: float,
                               region: Optional[str]
                               ) -> Tuple[List[List[float]], List[float], List[Dict[str, Any]]]:
    """(X, y, used) — one row per historical invoice with a known kWh/day:
    X=[hdd_mean, cdd_mean, calendar_frac] for its OWN period, y=kwh/day.
    Fetches every invoice's degree-days concurrently (api_exo, cached)."""
    comps = [components_for(inv) for inv in hist]
    valid = [(inv, c) for inv, c in zip(hist, comps)
            if c["days"] and c["energy_kwh"]]
    feats = await asyncio.gather(*(
        _period_features(lat, lon, c["period_start"], c["period_end"], region)
        for _inv, c in valid))
    X: List[List[float]] = []
    y: List[float] = []
    used: List[Dict[str, Any]] = []
    for (inv, c), feat in zip(valid, feats):
        if feat is None:
            continue
        X.append([feat["hdd"], feat["cdd"], feat["calendar"]])
        y.append(c["energy_kwh"] / c["days"])
        used.append(inv)
    return X, y, used


def _band_fraction(hist: List[Dict[str, Any]]) -> Dict[str, float]:
    """Household's own historical P1/P2/P3 kWh split (fallback: equal
    thirds) — used to weight the expected €/kWh across bands without
    needing an hourly load shape (bills are a period total, not a curve)."""
    p1 = p2 = p3 = 0.0
    for inv in hist:
        p1 += inv.get("energy_p1_kwh") or 0
        p2 += inv.get("energy_p2_kwh") or 0
        p3 += inv.get("energy_p3_kwh") or 0
    tot = p1 + p2 + p3
    if tot <= 0:
        return {"P1": 1 / 3, "P2": 1 / 3, "P3": 1 / 3}
    return {"P1": p1 / tot, "P2": p2 / tot, "P3": p3 / tot}


async def _price_expected_mean(slugs: Sequence[str], start: str, end: str,
                               supply_point_id: Optional[int],
                               band_frac: Dict[str, float]) -> Optional[float]:
    """Blended €/kWh for [start, end] under the CONTRACT the member is
    ACTUALLY billed on today (same tariff — spec requirement), weighted by
    the household's own historical band split. Falls back to a flat mean of
    every hour's price when the band breakdown isn't available (no
    schedule/contract data — shouldn't happen given pricing.py's own PVPC
    fallback, but never raise over pricing data)."""
    pmap, _label = await pricing.hourly_price_map_for_slugs(slugs, start, end, supply_point_id)
    if not pmap:
        return None
    by_band: Dict[str, List[float]] = {"P1": [], "P2": [], "P3": []}
    all_prices: List[float] = []
    for row in pmap.values():
        price = row.get("price_eur_kwh")
        if price is None:
            continue
        all_prices.append(price)
        band = row.get("period")
        if band in by_band:
            by_band[band].append(price)
    known = {b: (sum(v) / len(v)) for b, v in by_band.items() if v}
    if not known:
        return (sum(all_prices) / len(all_prices)) if all_prices else None
    wsum = sum(band_frac.get(b, 0.0) for b in known)
    if wsum <= 0:
        return sum(known.values()) / len(known)
    return sum(band_frac.get(b, 0.0) * p for b, p in known.items()) / wsum


async def _last_year_ref(hist: List[Dict[str, Any]], period_start: str, period_end: str,
                         price_expected_mean: Optional[float]) -> Optional[Dict[str, Any]]:
    """Same-period-last-year: the historical invoice whose period is
    closest to exactly 365 days before this one, TARIFF-REINDEXED to
    CURRENT prices (its OWN kWh × TODAY's blended €/kWh) so a pure price
    move doesn't masquerade as a consumption change. Informational
    corroboration only (spec: "flag on the stronger signal" = the
    weather-adjusted model) — not decomposed into its own driver waterfall."""
    target = date.fromisoformat(period_start)
    best, best_dist = None, None
    for inv in hist:
        c = components_for(inv)
        if not c["days"] or not c["energy_kwh"]:
            continue
        mid = date.fromisoformat(c["period_start"])
        dist = abs((mid.replace(year=target.year) if mid.month != 2 or mid.day != 29
                    else mid.replace(year=target.year, day=28)) - target)
        if best is None or dist < best_dist:
            best, best_dist = (inv, c), dist
    if best is None or best_dist.days > 60:   # only accept a genuine ~1yr match
        return None
    inv, c = best
    reindexed_eur = (round(c["energy_kwh"] * price_expected_mean, 2)
                     if price_expected_mean is not None else None)
    return {"invoice_id": inv["id"], "period_start": c["period_start"], "period_end": c["period_end"],
            "kwh": c["energy_kwh"], "eur_then": c["total_eur"],
            "eur_reindexed_now": reindexed_eur}


async def expected_bill(customer_id: str, slugs: Sequence[str], cups: Optional[str],
                        period_start: str, period_end: str,
                        supply_point_id: Optional[int] = None,
                        exclude_invoice_id: Optional[int] = None,
                        equipment: Optional[Dict[str, bool]] = None,
                        region: Optional[str] = None) -> Dict[str, Any]:
    """The expected bill for [period_start, period_end] at this CUPS, built
    from the household's OWN invoice history (works with zero PLC data).

    Returns {status, hist_count, sparse, days,
             expected_kwh, expected_energy_eur, price_expected_eur_kwh,
             attribution: {base, contributions, total} (kWh, period total),
             expected_power_eur, expected_fixed_eur, expected_tax_eur,
             expected_total_eur, refs: {last_year, weather_model},
             features (this period's own hdd/cdd/calendar),
             means (the fit's x_bar)}.
    `status`: 'ok' | 'insufficient_data' (< MIN_HIST_FOR_ANY invoices)."""
    days = _days({"period_start": period_start, "period_end": period_end})
    out: Dict[str, Any] = {"status": "insufficient_data", "hist_count": 0, "sparse": True,
                           "days": days, "expected_kwh": None, "expected_energy_eur": None,
                           "price_expected_eur_kwh": None, "attribution": None,
                           "expected_power_eur": None, "expected_fixed_eur": None,
                           "expected_tax_eur": None, "expected_total_eur": None,
                           "refs": {"last_year": None, "weather_model": False},
                           "features": None, "means": None}
    if not days:
        return out

    hist = await invoices_svc.history_for_cups(customer_id, cups, before_end=period_end,
                                               exclude_id=exclude_invoice_id)
    out["hist_count"] = len(hist)
    if len(hist) < MIN_HIST_FOR_ANY:
        return out
    out["status"] = "ok"
    out["sparse"] = len(hist) < MIN_HIST_FOR_REGRESSION

    loc = await consumption.location_for(slugs)
    lat = (loc or {}).get("lat")
    lon = (loc or {}).get("lon")
    from app.core.config import settings as _settings
    lat = lat if lat is not None else _settings.DEFAULT_LAT
    lon = lon if lon is not None else _settings.DEFAULT_LON

    band_frac = _band_fraction(hist)
    price_task = _price_expected_mean(slugs, period_start, period_end, supply_point_id, band_frac)
    period_feat_task = _period_features(lat, lon, period_start, period_end, region)
    price_expected_mean, period_feat = await asyncio.gather(price_task, period_feat_task)
    out["price_expected_eur_kwh"] = round(price_expected_mean, 5) if price_expected_mean is not None else None
    out["features"] = period_feat

    hist_components = [components_for(inv) for inv in hist]

    # ── Energy: degree-day + calendar OLS at INVOICE granularity, else a
    # flat historical mean of kWh/day (sparse/no-weather-data fallback). ──
    X, y, used = ([], [], [])
    if period_feat is not None and len(hist) >= MIN_HIST_FOR_REGRESSION:
        X, y, used = await _hist_energy_samples(hist, lat, lon, region)
    if len(y) >= MIN_HIST_FOR_REGRESSION:
        fit = _fit_variance_safe(X, y, equipment=equipment)
    else:
        fit = None
    kwh_day_samples = [c["energy_kwh"] / c["days"] for c in hist_components
                       if c["days"] and c["energy_kwh"]]
    if fit is not None:
        out["refs"]["weather_model"] = True
        n = len(y)
        x_bar = {"hdd": sum(r[0] for r in X) / n, "cdd": sum(r[1] for r in X) / n,
                 "calendar": sum(r[2] for r in X) / n}
        active = ["calendar"]
        if equipment is None or equipment.get("electric_heating", False):
            active.append("hdd")
        if equipment is None or equipment.get("electric_cooling", False):
            active.append("cdd")
        coefs_active = {k: fit[k] for k in active}
        x_bar_active = {k: x_bar[k] for k in active}
        x_period = {k: period_feat[k] for k in active}
        out["means"] = x_bar
        attribution = shap_attr.attribute_forecast(
            fit["intercept"], coefs_active, x_period, x_bar_active,
            labels=_FEATURE_LABELS, units=_FEATURE_UNITS)
        # Scale the per-day attribution to the PERIOD total (linear scaling
        # preserves base+sum(phi)==total exactly).
        attribution["base"] = round(attribution["base"] * days, 3)
        for c in attribution["contributions"]:
            c["phi"] = round(c["phi"] * days, 3)
            c["unit"] = "kWh"
        attribution["total"] = round(attribution["total"] * days, 3)
        expected_kwh = max(attribution["total"], 0.0)
    elif kwh_day_samples:
        mean_kwh_day = sum(kwh_day_samples) / len(kwh_day_samples)
        expected_kwh = round(mean_kwh_day * days, 3)
        attribution = {"base": expected_kwh, "contributions": [], "total": expected_kwh}
    else:
        expected_kwh = None
        attribution = None

    out["attribution"] = attribution
    out["expected_kwh"] = expected_kwh
    if expected_kwh is not None and price_expected_mean is not None:
        out["expected_energy_eur"] = round(expected_kwh * price_expected_mean, 2)

    # ── Power/fixed/tax: flat historical mean €/day (not weather-dependent),
    # None when NO historical invoice has that field knowable (uploaded
    # bills without an AI extraction) — caller must not treat as zero. ──
    for field, out_key in (("power_eur", "expected_power_eur"),
                          ("fixed_eur", "expected_fixed_eur"),
                          ("tax_eur", "expected_tax_eur")):
        vals = [c[field] / c["days"] for c in hist_components
               if c["days"] and c.get(field) is not None]
        out[out_key] = round((sum(vals) / len(vals)) * days, 2) if vals else None

    known_total_parts = [v for v in (out["expected_energy_eur"], out["expected_power_eur"],
                                     out["expected_fixed_eur"], out["expected_tax_eur"]) if v is not None]
    out["expected_total_eur"] = round(sum(known_total_parts), 2) if known_total_parts else None

    out["refs"]["last_year"] = (
        await _last_year_ref(hist, period_start, period_end, price_expected_mean)
        if not out["sparse"] else None)  # spec: sparse baseline → suppress the last-year ref
    return out
