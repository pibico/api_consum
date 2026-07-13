"""Tariff catalog (consum.tariff_catalog, mig 004) — reusable market products.

Reads are org-agnostic (the whole catalog is shared reference data); writes are
admin-only (populate/edit from the AI extraction or by hand). A household picks
retailer → product and the components prefill their contract, so nobody has to
understand P1/P2/P3, peajes or margins.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional

import psycopg
from psycopg.types.json import Jsonb
from fastapi import HTTPException

from app.core import db

logger = logging.getLogger("consum.tariff_catalog")

# Column set the API may write (id/updated_at are managed; retailer/product req).
_COLS = (
    "retailer", "product_name", "contract_type", "access_tariff",
    "energy_p1_eur_kwh", "energy_p2_eur_kwh", "energy_p3_eur_kwh",
    "margin_eur_kwh", "passthru_p1_eur_kwh", "passthru_p2_eur_kwh",
    "passthru_p3_eur_kwh", "power_p1_eur_kw_day", "power_p2_eur_kw_day",
    "meter_rental_eur_month", "electricity_tax_pct", "vat_pct", "components",
    "source_url", "valid_from", "confidence", "notes", "active",
)


def _adapt(col: str, val: Any) -> Any:
    """Wrap JSONB columns for psycopg3 (dict → Jsonb)."""
    if col == "components" and val is not None:
        return Jsonb(val)
    return val
_SELECT = f"SELECT id, {', '.join(_COLS)}, updated_by, updated_at FROM consum.tariff_catalog"


def _row(r) -> Dict[str, Any]:
    d: Dict[str, Any] = {"id": r[0]}
    for i, c in enumerate(_COLS, start=1):
        v = r[i]
        if isinstance(v, Decimal):
            v = float(v)
        elif hasattr(v, "isoformat"):       # valid_from (date)
            v = v.isoformat()
        d[c] = v
    d["updated_by"] = r[len(_COLS) + 1]
    ua = r[len(_COLS) + 2]
    d["updated_at"] = ua.isoformat() if ua else None
    return d


async def list_retailers() -> List[str]:
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                "SELECT DISTINCT retailer FROM consum.tariff_catalog "
                "WHERE active ORDER BY retailer")
            return [r[0] for r in await cur.fetchall()]


async def list_products(retailer: Optional[str] = None) -> List[Dict[str, Any]]:
    sql = _SELECT + " WHERE active"
    params: List[Any] = []
    if retailer:
        sql += " AND retailer = %s"
        params.append(retailer)
    sql += " ORDER BY retailer, product_name"
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(sql, params)
            return [_row(r) for r in await cur.fetchall()]


async def get(cid: int) -> Optional[Dict[str, Any]]:
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(_SELECT + " WHERE id = %s", (cid,))
            r = await cur.fetchone()
    return _row(r) if r else None


async def create(payload: Dict[str, Any], updated_by: Optional[str]) -> Dict[str, Any]:
    cols = [c for c in _COLS if c in payload and payload[c] is not None]
    if "retailer" not in cols or "product_name" not in cols:
        raise HTTPException(422, detail="retailer y product_name son obligatorios")
    values = [_adapt(c, payload[c]) for c in cols]
    sql = (f"INSERT INTO consum.tariff_catalog (updated_by, {', '.join(cols)}) "
           f"VALUES (%s, {', '.join(['%s'] * len(cols))}) RETURNING id")
    try:
        async with db.raw_connection() as con:
            async with con.cursor() as cur:
                await cur.execute(sql, [updated_by] + values)
                new_id = (await cur.fetchone())[0]
    except psycopg.errors.UniqueViolation:
        raise HTTPException(409, detail="Ya existe ese producto para esa comercializadora")
    logger.info("catalog product %s created by %s", new_id, updated_by)
    return await get(new_id)


async def update(cid: int, payload: Dict[str, Any], updated_by: Optional[str]) -> Dict[str, Any]:
    cols = [c for c in _COLS if c in payload]
    if not cols:
        return await get(cid)
    sets = ", ".join(f"{c} = %s" for c in cols) + ", updated_by = %s, updated_at = now()"
    values = [_adapt(c, payload[c]) for c in cols] + [updated_by, cid]
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                f"UPDATE consum.tariff_catalog SET {sets} WHERE id = %s", values)
            if cur.rowcount == 0:
                raise HTTPException(404, detail="Producto no encontrado")
    return await get(cid)


async def delete(cid: int) -> None:
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute("DELETE FROM consum.tariff_catalog WHERE id = %s", (cid,))
            if cur.rowcount == 0:
                raise HTTPException(404, detail="Producto no encontrado")


def _norm(s: Optional[str]) -> str:
    """Loose match key: lowercase, collapsed spaces, no accents/punctuation."""
    if not s:
        return ""
    import re
    import unicodedata
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).strip()


def _contains(a: str, b: str) -> bool:
    """Normalized containment either way ('masnorte' ↔ 'masnorte global power sl')."""
    return bool(a and b) and (a in b or b in a)


async def resolve(retailer: Optional[str], product: Optional[str]) -> Optional[Dict[str, Any]]:
    """Match an AI-extracted retailer/product against the catalog so the upload
    flow can settle the CONTRACT TYPE without asking (an invoice's month-average
    prices look 'fixed' even on indexed products — the catalog knows better).

    Confident only: exact-ish product match, or a retailer with a SINGLE active
    product. Anything fuzzier returns None and the flow asks the plain question."""
    r_key, p_key = _norm(retailer), _norm(product)
    if not r_key:
        return None
    rows = await list_products()
    by_retailer = [row for row in rows if _contains(r_key, _norm(row["retailer"]))]
    if not by_retailer:
        return None
    if p_key:
        for row in by_retailer:
            if _contains(p_key, _norm(row["product_name"])):
                return {"match": row, "matched_by": "product"}
    if len({row["id"] for row in by_retailer}) == 1:
        return {"match": by_retailer[0], "matched_by": "retailer_single"}
    return None
