"""OE3 cross-analysis engine (F3) — the CONSUM-IA R&D core.

Crosses the household's real consumption (shared Timescale) with the
exogenous variables served by api_exo, producing three insights:

1. **shift** — real cost vs the cost of the SAME daily energy with its
   flexible share (default 30%: washing machine, dishwasher, DHW…) moved
   to the day's cheapest PVPC hours → "ahorro potencial €/mes".
2. **thermal** — OLS regression consumo_dia ~ HDD + CDD (api_exo
   degree-days) → how much of the recent consumption swing is explained
   by weather rather than habits.
3. **window** — tomorrow's (fallback today's) cheap+sunny hours, scored
   from PVPC + solar GHI → recommended run-window for deferrable loads.

Insights are persisted daily to the local SQLite (`insights` table,
consum.db — same file the family-notify subscriber uses) as the history
that will feed the AI narrative in F4. AI only ever sees these AGGREGATES,
never raw sensor rows.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import date, datetime, time, timedelta
from typing import Any, Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

from app.core.config import settings
from app.services import consumption, exo_client, shap_attr

logger = logging.getLogger("consum.oe3")

MADRID_TZ = ZoneInfo("Europe/Madrid")

# Rank for picking the WORST (not best) confidence across a day's 24 hourly
# WS-EXO-PRICE rows — see forecast_explained's days-2-7 branch.
_CONFIDENCE_RANK = {"high": 0, "medium": 1, "low": 2}


async def _none():
    """Placeholder coroutine for an asyncio.gather slot that's a no-op this
    call (e.g. the price forecast on Cadence A / days_out=1, which never
    needs days 2-7)."""
    return None


def _day_bounds_local(d: str) -> tuple[datetime, datetime]:
    """(from, to) tz-aware Europe/Madrid datetimes spanning the full local day
    `d` (00:00 -> next day 00:00) — NEVER UTC (the toISOString "today" bug the
    owner has flagged before). `to` is exclusive/next-midnight, matching how a
    person reads "today": 00:00 through 24:00."""
    d0 = date.fromisoformat(d)
    start = datetime.combine(d0, time(0, 0), tzinfo=MADRID_TZ)
    end = datetime.combine(d0 + timedelta(days=1), time(0, 0), tzinfo=MADRID_TZ)
    return start, end


def _fmt_dt(dt: datetime) -> str:
    return dt.strftime("%d/%m %H:%M")


def _range_label(dt_from: datetime, dt_to: datetime) -> str:
    """Human 'from -> to' range — the mandatory explicit period every forecast
    must carry (owner requirement 2026-07-28): 'DD/MM HH:MM -> DD/MM HH:MM'."""
    return f"{_fmt_dt(dt_from)} → {_fmt_dt(dt_to)}"


def _hour_range_local(d: str, h_from: int, h_to: int) -> Dict[str, Any]:
    """One explicit hour-block window (e.g. the cheapest shift-tip hours) on
    local date `d`: {from, to} ISO-8601 WITH the Europe/Madrid UTC offset,
    plus a human label. `h_to` may be 24 (midnight rollover -> next day)."""
    d0 = date.fromisoformat(d)
    start = datetime.combine(d0, time(h_from % 24), tzinfo=MADRID_TZ)
    if h_from >= 24:
        start += timedelta(days=1)
    end_day = d0 + timedelta(days=1) if h_to >= 24 else d0
    end = datetime.combine(end_day, time(h_to % 24), tzinfo=MADRID_TZ)
    return {"from": start.isoformat(), "to": end.isoformat(),
           "label": f"{_fmt_dt(start)} → {end.strftime('%H:%M')}"
                   if start.date() == end.date() else _range_label(start, end)}

FLEX_SHARE = 0.30   # share of daily energy assumed shiftable (v1 heuristic)
CHEAP_HOURS = 6     # flexible energy is spread over the N cheapest hours
PRICE_WEIGHT = 0.6  # window score: price weighs more than solar
SOLAR_WEIGHT = 0.4

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _daily_hourly(rows: List[Dict[str, Any]]) -> Dict[str, Dict[int, float]]:
    """energy_series hourly rows → {local_date: {hour: kwh}} (ts is local —
    same convention the F2 endpoints rely on)."""
    out: Dict[str, Dict[int, float]] = {}
    for r in rows:
        d, h = r["ts"][:10], int(r["ts"][11:13])
        out.setdefault(d, {})[h] = out.get(d, {}).get(h, 0.0) + r["kwh"]
    return out


# ---------------------------------------------------------------------------
# 1. Shift analysis — real vs optimal cost
# ---------------------------------------------------------------------------


async def shift_analysis(slugs: Sequence[str], device: Optional[str] = None,
                         days: int = 30, sp=None) -> Dict[str, Any]:
    end = date.today() - timedelta(days=1)          # complete days only
    start = end - timedelta(days=days - 1)
    rows = await consumption.energy_series(slugs, start.isoformat(),
                                           end.isoformat(), bucket="hour",
                                           device=device, sp=sp)
    from app.services import pricing
    prices, _ = await pricing.hourly_price_map_for_slugs(
        slugs, start.isoformat(), end.isoformat(),
        supply_point_id=(sp or {}).get("id"))
    per_day = _daily_hourly(rows)

    real_total = optimal_total = 0.0
    days_used = 0
    for d, hours in per_day.items():
        day_prices = {h: prices.get((d, h), {}).get("price_eur_kwh")
                      for h in range(24)}
        if not any(v is not None for v in day_prices.values()):
            continue
        kwh_day = sum(hours.values())
        if kwh_day <= 0:
            continue
        # Real cost — hours without a price are skipped in BOTH sides
        real = sum(k * day_prices[h] for h, k in hours.items()
                   if day_prices.get(h) is not None)
        # Optimal: fixed share stays where it is; flexible share is spread
        # evenly over the day's CHEAP_HOURS cheapest priced hours.
        priced = sorted((p, h) for h, p in day_prices.items() if p is not None)
        cheap = priced[:CHEAP_HOURS]
        if not cheap:
            continue
        fixed = sum(k * (1 - FLEX_SHARE) * day_prices[h]
                    for h, k in hours.items() if day_prices.get(h) is not None)
        flex_kwh = sum(k * FLEX_SHARE for h, k in hours.items()
                       if day_prices.get(h) is not None)
        flex = flex_kwh / len(cheap) * sum(p for p, _ in cheap)
        real_total += real
        optimal_total += fixed + flex
        days_used += 1

    saving = max(real_total - optimal_total, 0.0)
    return {
        "window_days": days_used,
        "flex_share": FLEX_SHARE,
        "cheap_hours": CHEAP_HOURS,
        "real_cost_eur": round(real_total, 2),
        "optimal_cost_eur": round(optimal_total, 2),
        "saving_eur": round(saving, 2),
        "saving_pct": round(saving / real_total * 100, 1) if real_total > 0 else 0.0,
        "saving_month_eur": round(saving / days_used * 30, 2) if days_used else 0.0,
    }


# ---------------------------------------------------------------------------
# 2. Thermal normalization — consumo_dia ~ HDD + CDD (OLS, pure python)
# ---------------------------------------------------------------------------


def _ols3(X: List[List[float]], y: List[float]) -> Optional[List[float]]:
    """Least squares for y = b0 + b1·x1 + b2·x2 via normal equations
    (3×3 Gaussian elimination — no numpy dependency for 45 points)."""
    n = len(y)
    A = [[0.0] * 3 for _ in range(3)]
    b = [0.0] * 3
    for i in range(n):
        row = [1.0, X[i][0], X[i][1]]
        for j in range(3):
            b[j] += row[j] * y[i]
            for k in range(3):
                A[j][k] += row[j] * row[k]
    # Gaussian elimination with partial pivoting
    M = [A[i] + [b[i]] for i in range(3)]
    for col in range(3):
        piv = max(range(col, 3), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-9:
            return None
        M[col], M[piv] = M[piv], M[col]
        for r in range(3):
            if r != col:
                f = M[r][col] / M[col][col]
                for c in range(col, 4):
                    M[r][c] -= f * M[col][c]
    return [M[i][3] / M[i][i] for i in range(3)]


async def thermal_analysis(slugs: Sequence[str], device: Optional[str] = None,
                           days: int = 45, sp=None) -> Dict[str, Any]:
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    rows_task = consumption.energy_series(slugs, start.isoformat(),
                                          end.isoformat(), bucket="hour",
                                          device=device, sp=sp)
    dd_task = exo_client.degree_days(settings.DEFAULT_LAT, settings.DEFAULT_LON,
                                     start.isoformat(), end.isoformat())
    rows, dd = await asyncio.gather(rows_task, dd_task)
    daily_kwh = {d: round(sum(h.values()), 3)
                 for d, h in _daily_hourly(rows).items()}
    dd_by_date = {r["date"]: r for r in (dd or {}).get("days") or []}

    X, y, series = [], [], []
    for d in sorted(daily_kwh):
        r = dd_by_date.get(d)
        if not r or daily_kwh[d] <= 0:
            continue
        X.append([r["hdd"], r["cdd"]])
        y.append(daily_kwh[d])
        series.append({"date": d, "kwh": daily_kwh[d],
                       "temp_mean": r["temp_mean"],
                       "hdd": r["hdd"], "cdd": r["cdd"]})

    out: Dict[str, Any] = {"days_used": len(y), "series": series,
                           "base_kwh": None, "kwh_per_hdd": None,
                           "kwh_per_cdd": None, "r2": None,
                           "weather_share_pct": None}
    if len(y) < 10:
        out["status"] = "insufficient_data"
        return out
    beta = _ols3(X, y)
    if beta is None:
        out["status"] = "singular"
        return out
    b0, b_hdd, b_cdd = beta
    mean_y = sum(y) / len(y)
    ss_tot = sum((v - mean_y) ** 2 for v in y)
    ss_res = 0.0
    for i, v in enumerate(y):
        pred = b0 + b_hdd * X[i][0] + b_cdd * X[i][1]
        series[i]["expected_kwh"] = round(max(pred, 0.0), 2)
        ss_res += (v - pred) ** 2
    r2 = max(0.0, 1 - ss_res / ss_tot) if ss_tot > 0 else 0.0
    out.update({
        "status": "ok",
        "base_kwh": round(b0, 2),          # weather-independent daily floor
        "kwh_per_hdd": round(b_hdd, 3),    # extra kWh per heating degree-day
        "kwh_per_cdd": round(b_cdd, 3),    # extra kWh per cooling degree-day
        "r2": round(r2, 3),
        "weather_share_pct": round(r2 * 100, 1),  # variance explained by climate
    })
    # Yesterday: actual vs weather-expected → "habits vs weather" verdict
    last = series[-1] if series else None
    if last and last.get("expected_kwh"):
        dev = (last["kwh"] - last["expected_kwh"]) / last["expected_kwh"] * 100
        out["yesterday"] = {"date": last["date"], "kwh": last["kwh"],
                            "expected_kwh": last["expected_kwh"],
                            "deviation_pct": round(dev, 1)}
    return out


# ---------------------------------------------------------------------------
# 2b. Explainable forecast (Phase 1, 2026-07-28) — extends the OLS above with
#     a day-type regressor and attributes the prediction via deterministic
#     Shapley (shap_attr.py). Feeds the daily/weekly advice email
#     (workers/advice_scheduler.py) and the "why" panel.
# ---------------------------------------------------------------------------

FEATURE_LABELS = {"hdd": "Frío (grados-día)", "cdd": "Calor (grados-día)",
                  "calendar": "Fin de semana / festivo"}
FEATURE_UNITS = {"hdd": "kWh/HDD", "cdd": "kWh/CDD", "calendar": "kWh"}
_ALERT_SEV_RANK = {"red": 0, "orange": 1, "yellow": 2, "info": 3}


def _apply_calendar_label(attribution: Dict[str, Any], day_type: int,
                         usage_type: Optional[str] = None) -> Dict[str, Any]:
    """Bug fix 2026-07-29 + reframe 2026-07-30. The 'calendar' driver's
    φ = β·(x−x̄) is nonzero on an ORDINARY WEEKDAY too — x=0 while the
    training-set mean x̄ includes weekends/holidays (~0.3) — AND its SIGN
    depends on the site: a home (β>0, more weekend consumption) has a
    NEGATIVE weekday φ, but a workplace (β<0, more weekday consumption) has
    a POSITIVE one. The pre-2026-07-30 code phrased this sign as "eleva/
    reduce tu gasto por ser día X", which reads as a correct-but-bizarre
    justification for a workplace ("laborable ELEVA tu gasto" is technically
    true but sounds like blame) and is actively wrong framing for a member —
    day-type is background CONTEXT, never the headline reason for the euros.

    So this now drops the phi-sign-dependent "eleva/reduce" wording
    ENTIRELY. label/detail become NEUTRAL and are tailored only by
    `usage_type` (from consumption.comfort_flex_for(), 'vivienda'|'oficina'
    |'mixto'|None) x day_type (`_day_type_series`'s 1=weekend/holiday,
    0=weekday) — informative ("patrón habitual de..."), never blame-phrased.
    `usage_type=None`/'mixto' gets the same plain, non-blaming labels
    regardless of site type. Adds `label_en`/`detail_en` (EN counterparts —
    the codebase's other driver labels are Spanish-only, a pre-existing gap
    out of scope here). Purely cosmetic: phi/x/x_bar/unit and the
    base+Σφ==total identity are untouched — the caller (AdviceSkill,
    advice.py) additionally demotes this driver to secondary/omittable
    context in the narrative, on top of this neutral relabeling."""
    for c in attribution.get("contributions") or []:
        if c["feature"] != "calendar":
            continue
        if day_type == 1:
            c["label"] = "Fin de semana / festivo"
            c["label_en"] = "Weekend / holiday"
            if usage_type == "vivienda":
                c["detail"] = "patrón habitual del hogar (más actividad en fin de semana/festivo)"
                c["detail_en"] = "usual home pattern (more activity on weekends/holidays)"
            elif usage_type == "oficina":
                c["detail"] = "fuera del horario habitual de actividad de este lugar de trabajo"
                c["detail_en"] = "outside this workplace's usual activity schedule"
            else:
                c["detail"] = "fin de semana o festivo (dato de contexto, no la causa principal del gasto)"
                c["detail_en"] = "weekend or holiday (background context, not the main driver of spend)"
        else:
            c["label"] = "Día laborable"
            c["label_en"] = "Weekday"
            if usage_type == "oficina":
                c["detail"] = "patrón habitual de un lugar de trabajo"
                c["detail_en"] = "usual pattern for a workplace"
            elif usage_type == "vivienda":
                c["detail"] = "patrón habitual del hogar (menos actividad en día laborable)"
                c["detail_en"] = "usual home pattern (less activity on weekdays)"
            else:
                c["detail"] = "día laborable (dato de contexto, no la causa principal del gasto)"
                c["detail_en"] = "weekday (background context, not the main driver of spend)"
        break
    return attribution


def _ols_general(X: List[List[float]], y: List[float]) -> Optional[List[float]]:
    """Least squares y = b0 + sum(b_i·x_i) via normal equations, Gaussian
    elimination with partial pivoting — the same numerically-safe pattern as
    `_ols3`, generalized to k regressors (k = len(X[0])) so the day-type
    feature can be added without duplicating the algorithm."""
    n = len(y)
    if n == 0 or not X or not X[0]:
        return None
    k = len(X[0]) + 1
    A = [[0.0] * k for _ in range(k)]
    b = [0.0] * k
    for i in range(n):
        row = [1.0] + list(X[i])
        for j in range(k):
            b[j] += row[j] * y[i]
            for c in range(k):
                A[j][c] += row[j] * row[c]
    M = [A[i] + [b[i]] for i in range(k)]
    for col in range(k):
        piv = max(range(col, k), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-9:
            return None
        M[col], M[piv] = M[piv], M[col]
        for r in range(k):
            if r != col:
                f = M[r][col] / M[col][col]
                for c in range(col, k + 1):
                    M[r][c] -= f * M[col][c]
    return [M[i][k] / M[i][i] for i in range(k)]


_FEATURE_ORDER = ("hdd", "cdd", "calendar")


def _fit_variance_safe(X: List[List[float]], y: List[float],
                       equipment: Optional[Dict[str, bool]] = None
                       ) -> Optional[Dict[str, float]]:
    """Fit y ~ hdd + cdd + calendar, but FIRST drop any regressor with ~zero
    variance in the sample (e.g. HDD is identically 0 every day in mid-summer
    — no heating degree-days at all) — a constant column makes the normal
    equations singular by construction, not a numerical accident. Dropped
    features simply get beta=0 (their Shapley phi is then always 0, honestly
    reflecting "this driver had no signal in the fitting window").

    `equipment` (hard requirement 2026-07-29, comfort_flex contract) ALSO
    force-drops hdd when `equipment["electric_heating"]` is falsy, and cdd
    when `equipment["electric_cooling"]` is falsy — REGARDLESS of variance —
    so a household that heats with gas / has no AC never has its model fit a
    weather correlation it has no electric mechanism to produce. This is the
    same "drop the regressor" mechanism as the variance guard, so the
    Σφ+base==total efficiency property holds automatically (the model
    literally never included that term). `equipment=None` behaves like
    {"electric_heating": True, "electric_cooling": True} (no forced drop —
    callers that don't pass it keep the pre-2026-07-29 behavior); pass an
    explicit dict from consumption.comfort_flex_for() to gate it.

    Returns {"intercept": b0, "hdd": .., "cdd": .., "calendar": ..} or None
    only if EVERY regressor is degenerate AND there are no observations."""
    n = len(y)
    if n == 0:
        return None
    equip = equipment or {}
    keep = []
    for j, feat in enumerate(_FEATURE_ORDER):
        if feat == "hdd" and equipment is not None and not equip.get("electric_heating", False):
            continue
        if feat == "cdd" and equipment is not None and not equip.get("electric_cooling", False):
            continue
        vals = [row[j] for row in X]
        mean = sum(vals) / n
        var = sum((v - mean) ** 2 for v in vals) / n
        if var > 1e-6:
            keep.append(j)
    if not keep:
        return {"intercept": sum(y) / n, "hdd": 0.0, "cdd": 0.0, "calendar": 0.0}
    X_kept = [[row[j] for j in keep] for row in X]
    beta = _ols_general(X_kept, y)
    if beta is None:
        return None
    out = {"intercept": beta[0], "hdd": 0.0, "cdd": 0.0, "calendar": 0.0}
    for idx, j in enumerate(keep):
        out[_FEATURE_ORDER[j]] = beta[idx + 1]
    return out


async def _day_type_series(dates: Sequence[str],
                           region: Optional[str]) -> Dict[str, int]:
    """date -> 1 if weekend OR holiday (national+regional, S4 "behaviour"
    scope) else 0. Pricing (period_map/tariff_schedule_week) already applies
    NATIONAL-only holidays server-side in api_exo — untouched here."""
    if not dates:
        return {}
    years = sorted({int(d[:4]) for d in dates})
    hset: set = set()
    for yr in years:
        data = await exo_client.holidays(yr, region)
        for h in (data or {}).get("holidays") or []:
            d = h.get("date")
            if d:
                hset.add(d)
    out = {}
    for d in dates:
        wd = date.fromisoformat(d).weekday()
        out[d] = 1 if (wd >= 5 or d in hset) else 0
    return out


async def _hourly_shape(slugs: Sequence[str], device: Optional[str] = None,
                        sp=None, days: int = 14) -> Dict[int, float]:
    """Average fraction of daily energy consumed each hour, over the last
    `days` complete days — the household's own load SHAPE (not the market's),
    used to spread a forecast daily kWh total across hours for €-pricing.
    Falls back to a flat 1/24 shape when there is no recent data."""
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    rows = await consumption.energy_series(slugs, start.isoformat(),
                                           end.isoformat(), bucket="hour",
                                           device=device, sp=sp)
    per_day = _daily_hourly(rows)
    totals = [0.0] * 24
    n_days = 0
    for _d, hours in per_day.items():
        day_kwh = sum(hours.values())
        if day_kwh <= 0:
            continue
        n_days += 1
        for h in range(24):
            totals[h] += hours.get(h, 0.0) / day_kwh
    if n_days == 0:
        return {h: 1 / 24 for h in range(24)}
    return {h: totals[h] / n_days for h in range(24)}


async def _weather_guardrail(lat: float, lon: float, forecast_dates: Sequence[str],
                             dd_by_date: Dict[str, dict]) -> Dict[str, Dict[str, Any]]:
    """date -> {alert: {...}|None, normals_dev_c: float|None} (S5). An
    orange/red alert or a >=5°C deviation from the 30-yr normal on that date
    is the GUARDRAIL: forecast_explained folds HDD/CDD into one `weather`
    driver so the narrative attributes the swing to climate, not appliances."""
    alerts_task = exo_client.weather_alerts(lat, lon)
    normals_task = exo_client.climate_normals(
        lat, lon, days_before=0, days_after=max(len(forecast_dates) - 1, 0))
    alerts_data, normals_data = await asyncio.gather(alerts_task, normals_task)

    alerts_by_date: Dict[str, dict] = {}
    for a in (alerts_data or {}).get("alerts") or []:
        d = a.get("date") or str(a.get("peak_ts") or "")[:10]
        if not d:
            continue
        cur = alerts_by_date.get(d)
        if cur is None or _ALERT_SEV_RANK.get(a.get("severity"), 9) < \
                _ALERT_SEV_RANK.get(cur.get("severity"), 9):
            alerts_by_date[d] = a
    normals_by_date = {n["date"]: n for n in (normals_data or {}).get("normals") or []}

    out: Dict[str, Dict[str, Any]] = {}
    for d in forecast_dates:
        a = alerts_by_date.get(d)
        dev = None
        norm = normals_by_date.get(d)
        r = dd_by_date.get(d)
        if norm and norm.get("temp_mean") is not None and r and r.get("temp_mean") is not None:
            dev = round(r["temp_mean"] - norm["temp_mean"], 1)
        out[d] = {
            "alert": a if (a or {}).get("severity") in ("orange", "red") else None,
            "normals_dev_c": dev,
        }
    return out


def _consolidate_weather_driver(attribution: Dict[str, Any]) -> Dict[str, Any]:
    """Merge the hdd+cdd contributions into a single `weather` driver — used
    when the guardrail (S5) confirms an active alert/normals deviation, so
    the LLM narrates ONE clear "weather" cause instead of two jargon-y
    degree-day figures. Total is unaffected (pure re-grouping)."""
    contribs = attribution["contributions"]
    weather_phi = sum(c["phi"] for c in contribs if c["feature"] in ("hdd", "cdd"))
    others = [c for c in contribs if c["feature"] not in ("hdd", "cdd")]
    weather_entry = {"feature": "weather", "label": "Clima (aviso activo)",
                     "phi": round(weather_phi, 4), "x": None, "x_bar": None,
                     "unit": "kWh"}
    attribution["contributions"] = sorted(
        others + [weather_entry], key=lambda c: -abs(c["phi"]))
    return attribution


def _periods_for_date(schedule: Optional[dict], d: str) -> List[str]:
    """Distinct P1/P2/P3 bands present on date `d` in a tariff_schedule_week
    response — the "period structure" S3 promises for days 2-7 even without
    a price level (calendar is certain; only the price € is deferred)."""
    seen: List[str] = []
    for row in (schedule or {}).get("rows") or []:
        dl = str(row.get("datetime_local") or "")
        if dl[:10] != d:
            continue
        band = row.get("band")
        if band and band not in seen:
            seen.append(band)
    return sorted(seen)


async def forecast_explained(slugs: Sequence[str], sp=None,
                             days_out: int = 1, region: Optional[str] = None,
                             device: Optional[str] = None,
                             hist_days: int = 45,
                             equipment: Optional[Dict[str, bool]] = None,
                             usage_type: Optional[str] = None) -> Dict[str, Any]:
    """Deterministic explainable forecast, `days_out` days ahead (1 = Cadence
    A / tomorrow; up to 7 = Cadence B). Fuses the forecast-month-style recent
    baseline with the thermal (HDD/CDD) + day-type OLS, attributes every
    forecast via exact Shapley (shap_attr.py), then prices day 1 with the
    REAL D+1 rate (S3) — days 2-7 get period structure only (price=None,
    the single swap site for the future `/prices/forecast` endpoint).

    `equipment` (hard requirement 2026-07-29): {"electric_heating": bool,
    "electric_cooling": bool} from consumption.comfort_flex_for()'s
    `equipment` sub-object — a household without electric heating/cooling
    NEVER gets the HDD/CDD driver in `attribution.contributions` (dropped
    from the regression entirely, not just hidden) and its numeric effect is
    excluded from the forecast total. `equipment=None` keeps the pre-gating
    behavior (both drivers active) for any caller that hasn't been updated
    yet — always pass it from the read/email paths.

    `usage_type` (2026-07-30, from consumption.comfort_flex_for(), 'vivienda'
    |'oficina'|'mixto'|None) reframes ONLY the 'calendar' driver's label/
    detail via `_apply_calendar_label` — the math (phi/base/total) is
    unaffected either way.

    Returns {status, days_used, r2, sigma_kwh, forecasts:[{date, kwh,
    band_lo, band_hi, eur, price_status, attribution, weather_guardrail,
    periods}], alert, equipment, usage_type} — `alert` is the single
    highest-severity orange/red weather alert in the window (S5 proactive
    advisory), or None.
    """
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=hist_days - 1)
    loc = await consumption.location_for(slugs, sp=sp)
    lat = (loc or {}).get("lat") or settings.DEFAULT_LAT
    lon = (loc or {}).get("lon") or settings.DEFAULT_LON

    last_fc_date = (date.today() + timedelta(days=days_out)).isoformat()
    rows_task = consumption.energy_series(slugs, start.isoformat(), end.isoformat(),
                                          bucket="hour", device=device, sp=sp)
    dd_task = exo_client.degree_days(lat, lon, start.isoformat(), last_fc_date)
    rows, dd = await asyncio.gather(rows_task, dd_task)

    daily_kwh = {d: round(sum(h.values()), 3) for d, h in _daily_hourly(rows).items()}
    dd_by_date = {r["date"]: r for r in (dd or {}).get("days") or []}
    hist_dates = sorted(d for d in daily_kwh if d <= end.isoformat())
    day_type_hist = await _day_type_series(hist_dates, region)

    X: List[List[float]] = []
    y: List[float] = []
    for d in hist_dates:
        r = dd_by_date.get(d)
        if not r or daily_kwh[d] <= 0:
            continue
        X.append([r["hdd"], r["cdd"], float(day_type_hist.get(d, 0))])
        y.append(daily_kwh[d])

    out: Dict[str, Any] = {"status": "insufficient_data", "days_used": len(y),
                           "forecasts": [], "alert": None, "equipment": equipment,
                           "usage_type": usage_type}
    if len(y) < 12:
        return out
    fit = _fit_variance_safe(X, y, equipment=equipment)
    if fit is None:
        out["status"] = "singular"
        return out
    b0 = fit["intercept"]
    coefs = {"hdd": fit["hdd"], "cdd": fit["cdd"], "calendar": fit["calendar"]}
    b_hdd, b_cdd, b_cal = coefs["hdd"], coefs["cdd"], coefs["calendar"]
    # Active-feature set for THIS household's equipment (hard requirement
    # 2026-07-29): drives both the driver list (attribute_forecast only
    # gets these keys — the excluded one never appears in `contributions`)
    # and whether _consolidate_weather_driver may fold a "weather" chip in.
    active_feats = ["calendar"]
    if equipment is None or equipment.get("electric_heating", False):
        active_feats.append("hdd")
    if equipment is None or equipment.get("electric_cooling", False):
        active_feats.append("cdd")
    n = len(y)
    x_bar = {"hdd": sum(r[0] for r in X) / n, "cdd": sum(r[1] for r in X) / n,
             "calendar": sum(r[2] for r in X) / n}
    ss_tot = sum((v - sum(y) / n) ** 2 for v in y)
    ss_res = 0.0
    for i in range(n):
        pred = b0 + b_hdd * X[i][0] + b_cdd * X[i][1] + b_cal * X[i][2]
        ss_res += (y[i] - pred) ** 2
    r2 = max(0.0, 1 - ss_res / ss_tot) if ss_tot > 0 else 0.0
    dof = max(n - 4, 1)
    sigma = (ss_res / dof) ** 0.5

    forecast_dates = [(date.today() + timedelta(days=i)).isoformat()
                      for i in range(1, days_out + 1)]
    day_type_fc = await _day_type_series(forecast_dates, region)
    shape_task = _hourly_shape(slugs, device=device, sp=sp)
    schedule_task = exo_client.tariff_schedule_week(forecast_dates[0])
    guardrail_task = _weather_guardrail(lat, lon, forecast_dates, dd_by_date)
    # WS-EXO-PRICE — one call covers the whole window; days 2-7 price the
    # single swap site below with it. None (api_exo down/cold) just means
    # those days keep showing "forecast_pending", same as before this landed.
    price_fc_task = exo_client.price_forecast(lat, lon, days=days_out) if days_out > 1 else _none()
    shape, schedule, guardrail, price_fc = await asyncio.gather(
        shape_task, schedule_task, guardrail_task, price_fc_task)
    price_fc_by_date: Dict[str, Dict[int, dict]] = {}
    for r in (price_fc or {}).get("hourly") or []:
        price_fc_by_date.setdefault(r["date"], {})[r["hour"]] = r

    from app.services import pricing  # local import: avoid a cycle (pricing -> ... -> oe3? none today, but matches shift_analysis's style)

    top_alert = None
    forecasts: List[Dict[str, Any]] = []
    # Filtered down to the household's active equipment (hard requirement
    # 2026-07-29) — attribute_forecast only iterates these keys, so an
    # excluded hdd/cdd NEVER appears in `contributions`, and base/total are
    # computed over the SAME reduced set (Σφ+base==total holds by
    # construction, same efficiency property as the full model).
    coefs_active = {k: coefs[k] for k in active_feats}
    x_bar_active = {k: x_bar[k] for k in active_feats}
    for i, d in enumerate(forecast_dates):
        r = dd_by_date.get(d) or {}
        x_full = {"hdd": r.get("hdd", 0.0), "cdd": r.get("cdd", 0.0),
                 "calendar": float(day_type_fc.get(d, 0))}
        x_active = {k: x_full[k] for k in active_feats}
        attribution = shap_attr.attribute_forecast(
            b0, coefs_active, x_active, x_bar_active, labels=FEATURE_LABELS, units=FEATURE_UNITS)
        # Bug fix 2026-07-29 + reframe 2026-07-30: relabel 'calendar' for
        # THIS date's actual day type (weekday vs weekend/holiday) AND this
        # household's usage_type — see _apply_calendar_label.
        attribution = _apply_calendar_label(attribution, int(day_type_fc.get(d, 0)), usage_type)
        gr = guardrail.get(d) or {}
        weather_hit = bool(gr.get("alert")) or (gr.get("normals_dev_c") is not None
                                                and abs(gr["normals_dev_c"]) >= 5)
        # Only fold hdd/cdd into a "weather" chip if at least one of them is
        # actually an active driver — a household with NEITHER electric
        # heating nor cooling must never get a "Clima" chip (there would be
        # nothing real to fold; it would just be a spurious 0.00 entry).
        if weather_hit and ("hdd" in active_feats or "cdd" in active_feats):
            attribution = _consolidate_weather_driver(attribution)
        kwh = max(attribution["total"], 0.0)
        d_from, d_to = _day_bounds_local(d)
        entry: Dict[str, Any] = {
            "date": d, "kwh": round(kwh, 2),
            "band_lo": round(max(kwh - 1.28 * sigma, 0.0), 2),
            "band_hi": round(kwh + 1.28 * sigma, 2),
            "attribution": attribution,
            "weather_guardrail": gr,
            "eur": None, "price_status": "pending", "periods": None,
            # Mandatory explicit period (owner requirement 2026-07-28): every
            # forecast states EXACTLY the date+hour window it covers, local
            # Europe/Madrid tz, never UTC.
            "period_from": d_from.isoformat(), "period_to": d_to.isoformat(),
            "period_label": _range_label(d_from, d_to),
            "cheap_window": None,
        }
        if i == 0:
            # Cadence A (and day-1 of Cadence B): REAL D+1 price (S3).
            pmap, price_source = await pricing.hourly_price_map_for_slugs(
                slugs, d, d, supply_point_id=(sp or {}).get("id"))
            if pmap:
                eur = sum(kwh * shape.get(h, 1 / 24) *
                         ((pmap.get((d, h)) or {}).get("price_eur_kwh") or 0.0)
                         for h in range(24))
                entry["eur"] = round(eur, 2)
                entry["price_status"] = "real"
                entry["price_source"] = price_source
                entry["periods"] = sorted({(pmap.get((d, h)) or {}).get("period")
                                          for h in range(24)
                                          if (pmap.get((d, h)) or {}).get("period")})
                # Cheapest contiguous 3h block (valle-shift tip) — computed
                # HERE, deterministically, so the exact from->to hour range is
                # guaranteed present regardless of what the LLM narrates.
                hourly_prices = [(pmap.get((d, h)) or {}).get("price_eur_kwh") for h in range(24)]
                if all(p is not None for p in hourly_prices):
                    best_h, best_avg = 0, None
                    for h in range(0, 22):
                        avg = sum(hourly_prices[h:h + 3]) / 3
                        if best_avg is None or avg < best_avg:
                            best_h, best_avg = h, avg
                    win = _hour_range_local(d, best_h, best_h + 3)
                    win["avg_price_eur_kwh"] = round(best_avg, 5)
                    entry["cheap_window"] = win
        else:
            # Days 2-7 (S3): period structure known (calendar-certain).
            # WS-EXO-PRICE swap site — real forecast_explained callers
            # (Cadence B, weekly) now get an estimated euro figure with a
            # confidence band + drivers; still "pending" only if api_exo's
            # forecast is unavailable (cold start, both providers down).
            entry["price_status"] = "forecast_pending"
            entry["periods"] = _periods_for_date(schedule, d)
            day_fc = price_fc_by_date.get(d)
            if day_fc and len(day_fc) == 24:
                eur = sum(
                    kwh * shape.get(h, 1 / 24) * (day_fc[h].get("price_mean_eur_kwh") or 0.0)
                    for h in range(24)
                )
                eur_lo = sum(
                    kwh * shape.get(h, 1 / 24) * (day_fc[h].get("price_lo_eur_kwh") or 0.0)
                    for h in range(24)
                )
                eur_hi = sum(
                    kwh * shape.get(h, 1 / 24) * (day_fc[h].get("price_hi_eur_kwh") or 0.0)
                    for h in range(24)
                )
                methods = {day_fc[h].get("method") for h in range(24)}
                confidences = [day_fc[h].get("confidence") for h in range(24)]
                entry["eur"] = round(eur, 2)
                entry["eur_lo"] = round(eur_lo, 2)
                entry["eur_hi"] = round(eur_hi, 2)
                entry["price_status"] = "forecast"
                entry["price_source"] = "exo_price_forecast"
                entry["price_method"] = "official" if "official" in methods else (
                    "proxy" if "proxy" in methods else "climatology")
                # Conservative: report the WORST confidence seen across the
                # day's 24 hours, not the best (a day with 1 low-confidence
                # hour and 23 medium ones is a "low confidence" day overall).
                entry["price_confidence"] = max(
                    confidences, key=lambda c: _CONFIDENCE_RANK.get(c, 9)) if confidences else None
                entry["periods"] = sorted({day_fc[h].get("period") for h in range(24)
                                          if day_fc[h].get("period")}) or entry["periods"]
        if gr.get("alert") and (top_alert is None or _ALERT_SEV_RANK.get(
                gr["alert"].get("severity"), 9) < _ALERT_SEV_RANK.get(
                top_alert.get("severity"), 9)):
            top_alert = gr["alert"]
        forecasts.append(entry)

    week_from, _ = _day_bounds_local(forecast_dates[0])
    _, week_to = _day_bounds_local(forecast_dates[-1])
    out.update({"status": "ok", "r2": round(r2, 3), "sigma_kwh": round(sigma, 2),
               "region": region, "forecasts": forecasts, "alert": top_alert,
               # Overall window covered by THIS call (owner requirement
               # 2026-07-28) — spans day 1's 00:00 to the last day's 24:00.
               "period_from": week_from.isoformat(), "period_to": week_to.isoformat(),
               "period_label": _range_label(week_from, week_to)})
    return out


# ---------------------------------------------------------------------------
# 3. Green window — cheap + sunny hours for tomorrow (fallback today)
# ---------------------------------------------------------------------------


async def green_window(lat: Optional[float] = None,
                       lon: Optional[float] = None) -> Dict[str, Any]:
    pvpc = await exo_client.pvpc_day("tomorrow")
    target_day = (date.today() + timedelta(days=1)).isoformat()
    if not pvpc or not pvpc.get("prices"):
        pvpc = await exo_client.pvpc_day("today")
        target_day = date.today().isoformat()
    solar = await exo_client.solar_forecast(lat or settings.DEFAULT_LAT,
                                            lon or settings.DEFAULT_LON)

    prices: Dict[int, Dict[str, Any]] = {}
    for p in (pvpc or {}).get("prices") or []:
        h = p.get("hour")
        if h is None:
            continue
        prices[int(h)] = {
            "price": p.get("price_eur_kwh") or ((p.get("price_eur_mwh") or 0) / 1000.0),
            "period": p.get("period"),
        }
    ghi: Dict[int, float] = {}
    for row in (solar or {}).get("hourly") or []:
        ts = str(row.get("ts") or "")
        if ts[:10] == target_day:
            ghi[int(ts[11:13])] = float(row.get("ghi") or 0)

    if not prices:
        return {"date": target_day, "status": "no_data", "hours": [], "best": None}

    vals = [v["price"] for v in prices.values()]
    p_min, p_max = min(vals), max(vals)
    g_max = max(ghi.values()) if ghi else 0.0
    hours: List[Dict[str, Any]] = []
    for h in range(24):
        if h not in prices:
            continue
        p = prices[h]
        p_norm = (p["price"] - p_min) / (p_max - p_min) if p_max > p_min else 0.0
        s_norm = (ghi.get(h, 0.0) / g_max) if g_max > 0 else 0.0
        score = PRICE_WEIGHT * (1 - p_norm) + SOLAR_WEIGHT * s_norm
        hours.append({"hour": h, "price_eur_kwh": round(p["price"], 4),
                      "period": p["period"], "ghi": round(ghi.get(h, 0.0)),
                      "score": round(score, 3)})
    # Best contiguous 3-hour window by mean score
    best = None
    for i in range(len(hours) - 2):
        w = hours[i:i + 3]
        if w[2]["hour"] - w[0]["hour"] != 2:
            continue
        s = sum(x["score"] for x in w) / 3
        if best is None or s > best["score"]:
            best = {"start": w[0]["hour"], "end": w[2]["hour"] + 1,
                    "score": round(s, 3),
                    "price_avg": round(sum(x["price_eur_kwh"] for x in w) / 3, 4)}
    return {"date": target_day, "status": "ok", "hours": hours, "best": best,
            "weights": {"price": PRICE_WEIGHT, "solar": SOLAR_WEIGHT}}


# ---------------------------------------------------------------------------
# Persistence — daily insight snapshots (SQLite consum.db, feeds F4's AI)
# ---------------------------------------------------------------------------


def _persist_sync(day: str, scope: str, kind: str, payload: dict) -> None:
    c = sqlite3.connect(settings.DB_PATH, timeout=10)
    try:
        c.execute("""CREATE TABLE IF NOT EXISTS insights (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day TEXT NOT NULL, scope TEXT NOT NULL, kind TEXT NOT NULL,
            payload TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(day, scope, kind))""")
        slim = {k: v for k, v in payload.items() if k not in ("series", "hours")}
        c.execute("INSERT OR REPLACE INTO insights (day, scope, kind, payload, created_at) "
                  "VALUES (?,?,?,?,?)",
                  (day, scope, kind, json.dumps(slim),
                   datetime.utcnow().isoformat(timespec="seconds") + "Z"))
        c.commit()
    finally:
        c.close()


async def persist_insights(scope: str, shift: dict, thermal: dict, window: dict) -> None:
    day = date.today().isoformat()
    try:
        await asyncio.to_thread(lambda: [
            _persist_sync(day, scope, "shift", shift),
            _persist_sync(day, scope, "thermal", thermal),
            _persist_sync(day, scope, "window", window),
        ])
    except Exception as e:  # persistence is best-effort, never breaks the page
        logger.warning("insight persist failed: %s", e)


# ---------------------------------------------------------------------------
# 4. Achieved savings — the HERO number: what the household actually paid this
#    month vs pricing the same energy at the month's EXPENSIVE-hour average.
#    The baseline is named in the UI copy ("comparado con hacer todo en horas
#    caras") — a savings figure without a stated counterfactual reads as noise.
# ---------------------------------------------------------------------------


async def achieved_savings(slugs: Sequence[str],
                           device: Optional[str] = None, sp=None) -> Dict[str, Any]:
    from app.services import pricing

    today = date.today()
    start = today.replace(day=1)
    if today.day < 2:                          # month just started — no story yet
        return {"status": "no_data"}
    end = today - timedelta(days=1)
    rows = await consumption.energy_series(slugs, start.isoformat(),
                                           end.isoformat(), bucket="hour",
                                           device=device, sp=sp)
    prices, source = await pricing.hourly_price_map_for_slugs(
        slugs, start.isoformat(), end.isoformat(),
        supply_point_id=(sp or {}).get("id"))
    real = kwh_tot = 0.0
    p1_prices: List[float] = []
    for r in rows:
        d, h = r["ts"][:10], int(r["ts"][11:13])
        p = prices.get((d, h)) or {}
        price = p.get("price_eur_kwh")
        if price is None:
            continue
        kwh = r["kwh"] or 0.0
        real += kwh * price
        kwh_tot += kwh
        if p.get("period") == "P1":
            p1_prices.append(price)
    if kwh_tot <= 0 or not p1_prices:
        return {"status": "no_data"}
    p1_avg = sum(p1_prices) / len(p1_prices)
    expensive = kwh_tot * p1_avg
    saving = max(expensive - real, 0.0)
    return {
        "status": "ok",
        "month": start.isoformat()[:7],
        "kwh": round(kwh_tot, 1),
        "real_eur": round(real, 2),
        "expensive_eur": round(expensive, 2),
        "saving_eur": round(saving, 2),
        "p1_avg_eur_kwh": round(p1_avg, 5),
        "avg_eur_kwh": round(real / kwh_tot, 5),
        "price_source": source,
    }


# ---------------------------------------------------------------------------
# 5. Appliance cost ranking — "qué te cuesta cada aparato" + the always-on
#    (standby) floor, the single most actionable finding in the field.
#    Reads the permanent hourly cagg (cheap); € uses the month's average
#    energy price (approximation, labeled as such in the UI).
# ---------------------------------------------------------------------------


async def appliance_costs(slugs: Sequence[str], sp=None) -> Dict[str, Any]:
    from app.core import db

    ids = await consumption._slugs_to_ids(slugs)
    if not ids:
        return {"status": "no_data", "items": []}
    sp_keys = None
    if sp is not None:
        from app.services import supply_points as _sps
        sp_keys = {d for d, _c in await _sps.subtree(sp)}
    today = date.today()
    start = today.replace(day=1)
    hours_elapsed = max(int((today - start).days) * 24, 1)
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            # ONLY sensors the PLC specifies (active registry rows) — rogue
            # publishers that reuse the home's topic prefix must never show.
            await cur.execute("""
                SELECT h.device_id,
                       SUM(h.avg) / 1000.0                                  AS kwh,
                       percentile_cont(0.05) WITHIN GROUP (ORDER BY h.avg)  AS floor_w
                FROM sensor_hourly h
                JOIN sensors s ON s.customer_id = h.customer_id
                              AND s.sensor_key = h.device_id
                              AND s.channel = h.channel AND s.is_active
                WHERE h.variable = 'apower' AND h.customer_id::text = ANY(%s)
                  AND h.channel LIKE 'switch:%%' AND h.bucket >= %s::timestamptz
                GROUP BY h.device_id ORDER BY kwh DESC
            """, (list(ids.values()), start.isoformat()))
            rows = await cur.fetchall()
    if sp_keys is not None:
        rows = [r for r in rows if r[0] in sp_keys]   # F3: solo el punto
    names = {d["id"]: d.get("name") or d["id"]
             for d in await consumption.sensor_devices(slugs)}

    # WIRING-AWARE de-nesting: a metered plug may hang BELOW another metered
    # plug (a strip/UPS feeding PCs) — its energy is already inside the
    # parent's reading. Using the synced wiring tree (sensors.parent, mig
    # 021) we subtract each child from its NEAREST measured ancestor, so the
    # bars are DISJOINT and their sum is honest.
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                "SELECT sensor_key, channel, parent FROM sensors "
                "WHERE customer_id::text = ANY(%s) AND parent IS NOT NULL "
                "AND parent <> ''", (list(ids.values()),))
            parent_of = {f"{k}/{c}": p for k, c, p in await cur.fetchall()}

    measured = {dev: [float(kwh or 0), float(floor_w or 0)]
                for dev, kwh, floor_w in rows if (kwh or 0) > 0.05}
    nested_in: Dict[str, str] = {}
    for dev in measured:
        node, hops = parent_of.get(f"{dev}/switch:0"), 0
        while node and hops < 10:                    # walk up the wiring tree
            anc = node.split("/", 1)[0]
            if anc in measured and anc != dev:
                nested_in[dev] = anc
                break
            node, hops = parent_of.get(node), hops + 1
    for child, anc in nested_in.items():             # make the bars disjoint
        measured[anc][0] = max(measured[anc][0] - measured[child][0], 0.0)
        measured[anc][1] = max(measured[anc][1] - measured[child][1], 0.0)

    items = []
    standby_kwh = 0.0
    for dev, kwh, floor_w in rows:
        if dev not in measured:
            continue
        kwh, floor_w = measured[dev]
        if kwh <= 0.05:
            continue
        st_kwh = floor_w * hours_elapsed / 1000.0 if floor_w >= 1.5 else 0.0
        standby_kwh += st_kwh
        items.append({"device": dev, "name": names.get(dev, dev),
                      "kwh": round(kwh, 1), "standby_w": round(floor_w, 1),
                      "nested_in": nested_in.get(dev),
                      "has_children": dev in set(nested_in.values())})
    items.sort(key=lambda it: -it["kwh"])
    return {"status": "ok", "month": start.isoformat()[:7],
            "items": items, "standby_kwh": round(standby_kwh, 1)}
