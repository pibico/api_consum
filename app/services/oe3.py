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
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence

from app.core.config import settings
from app.services import consumption, exo_client

logger = logging.getLogger("consum.oe3")

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
                         days: int = 30) -> Dict[str, Any]:
    end = date.today() - timedelta(days=1)          # complete days only
    start = end - timedelta(days=days - 1)
    rows = await consumption.energy_series(slugs, start.isoformat(),
                                           end.isoformat(), bucket="hour",
                                           device=device)
    prices = await exo_client.pvpc_map(start.isoformat(), end.isoformat())
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
                           days: int = 45) -> Dict[str, Any]:
    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    rows_task = consumption.energy_series(slugs, start.isoformat(),
                                          end.isoformat(), bucket="hour",
                                          device=device)
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
