"""Accuracy eval-harness admin endpoints (2026-07-30). Superadmin-only —
this exposes fleet-wide accuracy numbers across every household, which is
strictly an internal/observability concern (never a member-facing feature,
no flags flip from here). Mirrors the is_superadmin gate pattern used
elsewhere (e.g. plc.py's admin-only routes) rather than require_role, since
there is no per-household ownership concept for a cross-fleet metric.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.v1.dependencies.rbac import ConsumContext, consum_context
from app.core import db
from app.services import eval_harness

router = APIRouter(prefix="/eval", tags=["eval"])


def _require_superadmin(ctx: ConsumContext) -> None:
    if not ctx.is_superadmin:
        raise HTTPException(403, detail={
            "code": "SUPERADMIN_REQUIRED",
            "message": "El arnés de evaluación es solo para superadmin."})


@router.get("/metrics")
async def get_metrics(metric: Optional[str] = Query(None, description="metric_key filter"),
                      scope: Optional[str] = Query(None, description="'fleet'|'household'"),
                      slug: Optional[str] = Query(None, description="household slug filter"),
                      since: Optional[str] = Query(None, description="ISO date/datetime lower bound on computed_at"),
                      limit: int = Query(500, ge=1, le=5000),
                      ctx: ConsumContext = Depends(consum_context)):
    """Time series of accuracy metrics — the trend a human watches to decide
    when to flip a dark flag. Read-only, superadmin-gated (fleet-wide data)."""
    _require_superadmin(ctx)
    where = ["1=1"]
    params: list = []
    if metric:
        where.append("metric_key = %s")
        params.append(metric)
    if scope:
        where.append("scope = %s")
        params.append(scope)
    if slug:
        where.append("slug = %s")
        params.append(slug)
    if since:
        where.append("computed_at >= %s")
        params.append(since)
    params.append(limit)
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(f"""
                SELECT id, computed_at, metric_key, scope, slug, window_from, window_to,
                       horizon, value, unit, n, meta
                  FROM consum.eval_metrics
                 WHERE {' AND '.join(where)}
                 ORDER BY computed_at DESC
                 LIMIT %s
            """, params)
            rows = await cur.fetchall()
    return {"data": [{
        "id": r[0], "computed_at": r[1].isoformat(), "metric_key": r[2], "scope": r[3],
        "slug": r[4], "window_from": r[5].isoformat(), "window_to": r[6].isoformat(),
        "horizon": r[7], "value": r[8], "unit": r[9], "n": r[10], "meta": r[11],
    } for r in rows]}


@router.post("/run")
async def trigger_run(window_days: Optional[int] = Query(None, ge=1, le=365),
                      ctx: ConsumContext = Depends(consum_context)):
    """Manual trigger — compute every metric now, without waiting for
    Monday's scheduled run. Same code path as the scheduler
    (eval_harness.run_all); useful to validate after a migration/deploy."""
    _require_superadmin(ctx)
    summary = await eval_harness.run_all(window_days=window_days)
    return summary
