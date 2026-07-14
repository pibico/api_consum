"""Supply contracts (contrato de suministro) — api_consum's business domain.

CRUD + resolution over consum.contracts (see app/db/migrations/001_consum_schema.sql).
Tenancy is app-level: callers pass slugs already authorized by ConsumContext;
the DB enforces the one-active-contract-per-date rule (gist exclusion → 409).
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

import psycopg
from psycopg.types.json import Jsonb
from fastapi import HTTPException

from app.core import db
from app.services.consumption import _slugs_to_ids

logger = logging.getLogger("consum.contracts")

# Column list shared by every SELECT/INSERT/UPDATE — keep in lockstep with the DDL.
_COLS = (
    "customer_id", "contract_type", "label", "retailer", "cups", "supply_point_id",
    "access_tariff",
    "start_date", "end_date",
    "energy_p1_eur_kwh", "energy_p2_eur_kwh", "energy_p3_eur_kwh",
    "margin_eur_kwh",
    "passthru_p1_eur_kwh", "passthru_p2_eur_kwh", "passthru_p3_eur_kwh",
    "power_p1_kw", "power_p2_kw", "power_p1_eur_kw_day", "power_p2_eur_kw_day",
    "meter_rental_eur_month", "other_fixed_eur_month",
    "electricity_tax_pct", "vat_pct",
    "components", "notes", "created_by",
)
_MUTABLE = tuple(c for c in _COLS if c not in ("customer_id", "created_by"))


def _adapt(col: str, val: Any) -> Any:
    """JSONB columns need psycopg3's Jsonb wrapper (dict → jsonb)."""
    return Jsonb(val) if col == "components" and val is not None else val
_SELECT = ("SELECT c.id, cu.slug, " + ", ".join(f"c.{c}" for c in _COLS)
           + ", c.created_at, c.updated_at "
           "FROM consum.contracts c JOIN public.customers cu USING (customer_id)")

_NUMERIC = {
    "energy_p1_eur_kwh", "energy_p2_eur_kwh", "energy_p3_eur_kwh",
    "margin_eur_kwh",
    "passthru_p1_eur_kwh", "passthru_p2_eur_kwh", "passthru_p3_eur_kwh",
    "power_p1_kw", "power_p2_kw", "power_p1_eur_kw_day", "power_p2_eur_kw_day",
    "meter_rental_eur_month", "other_fixed_eur_month",
    "electricity_tax_pct", "vat_pct",
}


def _row_to_dict(row: Sequence[Any]) -> Dict[str, Any]:
    keys = ("id", "slug") + _COLS + ("created_at", "updated_at")
    out: Dict[str, Any] = {}
    for k, v in zip(keys, row):
        if k in _NUMERIC and v is not None:
            v = float(v)
        elif k in ("start_date", "end_date", "created_at", "updated_at") and v is not None:
            v = v.isoformat()
        elif k == "customer_id":
            v = str(v)
        out[k] = v
    return out


async def list_for(slugs: Sequence[str]) -> List[Dict[str, Any]]:
    if not slugs:
        return []
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                _SELECT + " WHERE cu.slug = ANY(%s) ORDER BY cu.slug, c.start_date DESC",
                (list(slugs),),
            )
            return [_row_to_dict(r) for r in await cur.fetchall()]


async def get(contract_id: int) -> Optional[Dict[str, Any]]:
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(_SELECT + " WHERE c.id = %s", (contract_id,))
            row = await cur.fetchone()
    return _row_to_dict(row) if row else None


async def get_active(customer_id: str, on_date: str,
                     supply_point_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """The contract covering `on_date` (YYYY-MM-DD), if any. Con
    `supply_point_id` (F3) prima el contrato de ESE punto; un contrato legado
    (supply_point_id NULL) sigue valiendo como fallback."""
    extra, params = "", [customer_id, on_date, on_date]
    if supply_point_id is not None:
        extra = " AND (c.supply_point_id = %s OR c.supply_point_id IS NULL)"
        params.append(supply_point_id)
    order = " ORDER BY (c.supply_point_id IS NOT NULL) DESC" if supply_point_id else ""
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                _SELECT + """ WHERE c.customer_id = %s AND c.start_date <= %s
                              AND (c.end_date IS NULL OR c.end_date >= %s)"""
                + extra + order + " LIMIT 1",
                params,
            )
            row = await cur.fetchone()
    return _row_to_dict(row) if row else None


async def contracts_covering(customer_id: str, start: str, end: str,
                             supply_point_id: Optional[int] = None
                             ) -> List[Dict[str, Any]]:
    """All contracts intersecting [start, end], ordered — feeds pricing.price_map
    when a contract changes mid-range. Con `supply_point_id` (F3): los del
    punto + los legados sin punto (fallback pre-F3)."""
    extra, params = "", [customer_id, end, start]
    if supply_point_id is not None:
        extra = " AND (c.supply_point_id = %s OR c.supply_point_id IS NULL)"
        params.append(supply_point_id)
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                _SELECT + """ WHERE c.customer_id = %s AND c.start_date <= %s
                              AND (c.end_date IS NULL OR c.end_date >= %s)"""
                + extra + " ORDER BY c.start_date",
                params,
            )
            return [_row_to_dict(r) for r in await cur.fetchall()]


async def resolve_for_slugs(slugs: Sequence[str], start: str, end: str,
                            supply_point_id: Optional[int] = None
                            ) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """(customer_id, contracts) — only when the slugs resolve to EXACTLY ONE
    customer. Multi-household aggregates cannot be costed per-contract (the
    kWh series is summed across homes) → (None, []) and the caller falls back
    to PVPC, which is the pre-contract behavior."""
    ids = set((await _slugs_to_ids(slugs)).values())
    if len(ids) != 1:
        return None, []
    cid = ids.pop()
    return cid, await contracts_covering(cid, start, end, supply_point_id)


def _http_409() -> HTTPException:
    return HTTPException(
        409, detail="Ya existe un contrato vigente que solapa con ese periodo")


async def create(customer_slug: str, payload: Dict[str, Any],
                 created_by: Optional[str]) -> Dict[str, Any]:
    ids = await _slugs_to_ids([customer_slug])
    cid = ids.get(customer_slug)
    if not cid:
        raise HTTPException(404, detail=f"Hogar desconocido: {customer_slug}")
    # Auto-vinculación por CUPS (F3, decisión 13-07): sin punto explícito,
    # el CUPS del contrato decide su punto de suministro (o lo adopta si hay
    # exactamente un punto sin CUPS). None → contrato legado (sin punto).
    if payload.get("supply_point_id") is None and payload.get("cups"):
        from app.services import supply_points as _sps
        payload["supply_point_id"] = await _sps.bind_contract_cups(
            cid, payload.get("cups"))
    cols = [c for c in _MUTABLE if payload.get(c) is not None]
    values = [_adapt(c, payload[c]) for c in cols]
    sql = (
        f"INSERT INTO consum.contracts (customer_id, created_by, {', '.join(cols)}) "
        f"VALUES (%s, %s, {', '.join(['%s'] * len(cols))}) RETURNING id"
    )
    try:
        async with db.raw_connection() as con:
            async with con.cursor() as cur:
                await cur.execute(sql, [cid, created_by] + values)
                new_id = (await cur.fetchone())[0]
    except psycopg.errors.ExclusionViolation:
        raise _http_409()
    except psycopg.errors.CheckViolation as exc:
        raise HTTPException(422, detail=f"Contrato inválido: {exc.diag.constraint_name}")
    logger.info("contract %s created for %s by %s", new_id, customer_slug, created_by)
    return await get(new_id)


async def update(contract_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    # NULLs are meaningful (e.g. clearing end_date reactivates a contract), so
    # the payload dict carries exactly the fields the caller wants to set.
    # Re-vinculación por CUPS (F3): si cambia el cups sin punto explícito,
    # el match decide (igual que en create).
    if payload.get("cups") and "supply_point_id" not in payload:
        existing = await get(contract_id)
        if existing:
            from app.services import supply_points as _sps
            bound = await _sps.bind_contract_cups(existing["customer_id"],
                                                  payload["cups"])
            if bound is not None:
                payload["supply_point_id"] = bound
    cols = [c for c in _MUTABLE if c in payload]
    if not cols:
        raise HTTPException(422, detail="Nada que actualizar")
    sets = ", ".join(f"{c} = %s" for c in cols)
    try:
        async with db.raw_connection() as con:
            async with con.cursor() as cur:
                await cur.execute(
                    f"UPDATE consum.contracts SET {sets}, updated_at = now() "
                    "WHERE id = %s RETURNING id",
                    [_adapt(c, payload[c]) for c in cols] + [contract_id],
                )
                row = await cur.fetchone()
    except psycopg.errors.ExclusionViolation:
        raise _http_409()
    except psycopg.errors.CheckViolation as exc:
        raise HTTPException(422, detail=f"Contrato inválido: {exc.diag.constraint_name}")
    if not row:
        raise HTTPException(404, detail="Contrato no encontrado")
    return await get(contract_id)


async def delete(contract_id: int) -> bool:
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute("DELETE FROM consum.contracts WHERE id = %s RETURNING id",
                              (contract_id,))
            return (await cur.fetchone()) is not None
