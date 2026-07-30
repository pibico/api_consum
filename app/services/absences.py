"""Ausencias / vacaciones declaradas por CUPS (2026-07-30) — the member says
"we're away from X to Y" for one supply point (installation), and every
prediction/advice/anomaly/bill channel treats the resulting consumption dip
as EXPECTED instead of a problem.

Store: `consum.absence_periods` (migration 018) — one row per declared
window, `supply_point_id -> consum.supply_points` (the CUPS), tz-aware
TIMESTAMPTZ bounds. Several rows per CUPS are allowed and MAY overlap; every
read here UNIFIES touching/overlapping intervals into disjoint windows before
handing them to a caller (gotcha: "un hogar declara 2 ausencias solapadas" must
be evaluated as ONE window, not double-counted).

Consumers (all in this codebase, none elsewhere):
  - `oe3.forecast_explained` — `day_overlap_fraction`/`label_for` turn a
    fetched interval list into a per-day standby prior (see STANDBY_FRACTION).
  - `ai/advice.py` (`AdviceSkill.narrate`) — checks `entry.get("ausencia")` to
    switch to a deterministic vacation-mode narrative (never alarmed).
  - `workers/anomaly_poller.py` (`_dispatch_new_anomalies`) — `active_at`
    suppresses a LOW-consumption anomaly's email inside a declared absence.
  - `bill_expectation.expected_bill` — `overlap_fraction` reduces the
    expected kWh proportional to the absence's share of the billed period.

Tenancy: every write/delete is scoped through `supply_points.get()`'s own
ownership check (customer_ids already authorized by rbac) — this module
never trusts a bare `supply_point_id` from the caller without that check
EXCEPT `delete()`, which re-checks ownership itself (the endpoint only has
an absence_id, not a pre-resolved supply point).
"""
from __future__ import annotations

import logging
from datetime import date as _date, datetime, time as _time, timedelta
from typing import Any, Dict, List, Optional, Sequence
from zoneinfo import ZoneInfo

from app.core import db

logger = logging.getLogger("consum.absences")

MADRID_TZ = ZoneInfo("Europe/Madrid")

# Vacation-mode floor: the fraction of a household's NORMAL forecast kWh that
# keeps running while it's away (fridge, router, alarm, standby loads) — a
# v1 heuristic constant, same style as oe3.py's FLEX_SHARE/CHEAP_HOURS.
# 1 - STANDBY_FRACTION = the "strong absence prior" reduction applied to a
# fully-covered day; a partially-covered day gets that reduction scaled by
# its overlap fraction (see day_overlap_fraction), so the effect is STRICTLY
# bounded to the declared window (spec gotcha).
STANDBY_FRACTION = 0.35

# Sane upper bound for a single declared window (spec: "validate... sane
# range") — long enough for an extended stay, short enough to catch a
# fat-fingered year.
MAX_ABSENCE_DAYS = 400


def _row_to_dict(r) -> Dict[str, Any]:
    return {
        "id": r[0], "supply_point_id": r[1],
        "starts_at": r[2], "ends_at": r[3],
        "label": r[4], "created_by": r[5], "created_at": r[6],
    }


_COLUMNS = "id, supply_point_id, starts_at, ends_at, label, created_by, created_at"


async def list_for(supply_point_id: int) -> List[Dict[str, Any]]:
    """All declared absences for ONE CUPS, newest-starting first — the
    member-facing list (`GET /absences?supply=`)."""
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                f"SELECT {_COLUMNS} FROM consum.absence_periods "
                "WHERE supply_point_id = %s ORDER BY starts_at DESC",
                (supply_point_id,))
            return [_row_to_dict(r) for r in await cur.fetchall()]


async def create(supply_point_id: int, starts_at: datetime, ends_at: datetime,
                 label: Optional[str], created_by: Optional[str]) -> Dict[str, Any]:
    """Insert one declared window. Caller (endpoint) has ALREADY validated
    tz-aware + starts<ends + sane range and resolved `supply_point_id`
    ownership via `supply_points.get()`."""
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                f"""INSERT INTO consum.absence_periods
                     (supply_point_id, starts_at, ends_at, label, created_by)
                   VALUES (%s, %s, %s, %s, %s)
                   RETURNING {_COLUMNS}""",
                (supply_point_id, starts_at, ends_at, (label or None), created_by))
            row = await cur.fetchone()
        await con.commit()
    logger.info("absence created: sp=%s %s -> %s", supply_point_id, starts_at, ends_at)
    return _row_to_dict(row)


async def delete(absence_id: int, customer_ids: Sequence[str]) -> bool:
    """Deletes ONLY when the absence's supply point belongs to an authorized
    customer (joined check — the endpoint only has an absence_id, so
    ownership is re-verified HERE, not pre-resolved like create()/list_for()).
    Returns False (404 upstream) on no-match, never raises."""
    if not customer_ids:
        return False
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                """DELETE FROM consum.absence_periods a
                     USING consum.supply_points sp
                    WHERE a.id = %s AND a.supply_point_id = sp.id
                      AND sp.customer_id::text = ANY(%s)""",
                (absence_id, [str(c) for c in customer_ids]))
            deleted = cur.rowcount > 0
        await con.commit()
    return deleted


def _unify(intervals: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge overlapping/touching [starts_at, ends_at) windows into disjoint
    ones (spec gotcha: several absences per CUPS may overlap — evaluate as
    ONE window). Input need not be sorted; output is sorted by starts_at."""
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda a: a["starts_at"])
    out: List[Dict[str, Any]] = [dict(ordered[0])]
    for cur in ordered[1:]:
        last = out[-1]
        if cur["starts_at"] <= last["ends_at"]:
            if cur["ends_at"] > last["ends_at"]:
                last["ends_at"] = cur["ends_at"]
            if cur.get("label") and not last.get("label"):
                last["label"] = cur["label"]
        else:
            out.append(dict(cur))
    return out


async def absences_for(customer_id: Optional[str], supply_point_id: Optional[int],
                       from_ts: datetime, to_ts: datetime) -> List[Dict[str, Any]]:
    """Unified absence intervals overlapping [from_ts, to_ts) for ONE CUPS
    (`supply_point_id` given), or ALL of the customer's CUPS when it's None
    (the household-wide aggregation case — e.g. the daily/weekly advice email
    cadence, which doesn't filter by `?supply=`). Returns [] when neither
    identifier is known (never guesses)."""
    if supply_point_id is None and not customer_id:
        return []
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            if supply_point_id is not None:
                await cur.execute(
                    f"SELECT {_COLUMNS} FROM consum.absence_periods "
                    "WHERE supply_point_id = %s AND starts_at < %s AND ends_at > %s "
                    "ORDER BY starts_at",
                    (supply_point_id, to_ts, from_ts))
            else:
                await cur.execute(
                    """SELECT a.id, a.supply_point_id, a.starts_at, a.ends_at,
                              a.label, a.created_by, a.created_at
                         FROM consum.absence_periods a
                         JOIN consum.supply_points sp ON sp.id = a.supply_point_id
                        WHERE sp.customer_id::text = %s
                          AND a.starts_at < %s AND a.ends_at > %s
                        ORDER BY a.starts_at""",
                    (str(customer_id), to_ts, from_ts))
            rows = await cur.fetchall()
    return _unify([_row_to_dict(r) for r in rows])


async def active_at(customer_id: Optional[str], supply_point_id: Optional[int],
                    ts: datetime) -> Optional[Dict[str, Any]]:
    """The absence interval covering the single instant `ts`, or None —
    point-in-time check for `anomaly_poller._dispatch_new_anomalies`."""
    hits = await absences_for(customer_id, supply_point_id, ts, ts)
    return hits[0] if hits else None


def day_overlap_fraction(intervals: List[Dict[str, Any]],
                         window_from: datetime, window_to: datetime) -> float:
    """Fraction (0..1) of [window_from, window_to) covered by the (already
    unified) `intervals` — pure/sync so `oe3.forecast_explained` can call it
    once per forecast day without a DB round trip per day (the interval list
    is fetched ONCE for the whole forecast window)."""
    total = (window_to - window_from).total_seconds()
    if total <= 0 or not intervals:
        return 0.0
    covered = 0.0
    for iv in intervals:
        s = max(iv["starts_at"], window_from)
        e = min(iv["ends_at"], window_to)
        if e > s:
            covered += (e - s).total_seconds()
    return min(covered / total, 1.0)


def label_for(intervals: List[Dict[str, Any]],
             window_from: datetime, window_to: datetime) -> Optional[str]:
    """Human label + explicit range for whichever declared window(s) overlap
    [window_from, window_to) — mandatory explicit period discipline (owner
    requirement 2026-07-28: every forecast/advisory states its exact
    date+hour range)."""
    hits = [iv for iv in intervals if iv["ends_at"] > window_from and iv["starts_at"] < window_to]
    if not hits:
        return None
    label = next((iv.get("label") for iv in hits if iv.get("label")), None) or "Ausencia"
    start = hits[0]["starts_at"].astimezone(MADRID_TZ)
    end = hits[-1]["ends_at"].astimezone(MADRID_TZ)
    return f"{label} ({start.strftime('%d/%m %H:%M')} → {end.strftime('%d/%m %H:%M')})"


async def overlap_fraction(customer_id: Optional[str], supply_point_id: Optional[int],
                           period_start: str, period_end: str) -> Dict[str, Any]:
    """Fraction of a BILLED period [period_start, period_end] (inclusive
    local dates, YYYY-MM-DD) covered by declared absences for this CUPS —
    `bill_expectation.expected_bill`'s proportional-reduction input (Phase
    2.5 §4d: "a low bill during a declared vacation isn't an anomaly").
    Returns {"fraction": 0..1, "periods": [{starts_at, ends_at, label}]}."""
    d0 = _date.fromisoformat(period_start)
    d1 = _date.fromisoformat(period_end)
    from_ts = datetime.combine(d0, _time(0, 0), tzinfo=MADRID_TZ)
    to_ts = datetime.combine(d1 + timedelta(days=1), _time(0, 0), tzinfo=MADRID_TZ)
    if to_ts <= from_ts:
        return {"fraction": 0.0, "periods": []}
    intervals = await absences_for(customer_id, supply_point_id, from_ts, to_ts)
    frac = day_overlap_fraction(intervals, from_ts, to_ts)
    return {
        "fraction": round(frac, 4),
        "periods": [{"starts_at": iv["starts_at"].isoformat(),
                     "ends_at": iv["ends_at"].isoformat(),
                     "label": iv.get("label")} for iv in intervals],
    }
