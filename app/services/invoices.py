"""Invoices (facturas) — closed billing periods for CONSUM-IA.

An invoice freezes a settlement: for the period it partitions the days by the
contract(s) covering them (a mid-period contract switch → one segment per
contract; days with no contract → a PVPC energy-only segment), costs each
segment via pricing.bill_breakdown (HOURLY settlement, matching the retailer),
sums the totals, and stores the whole thing — including a snapshot of each
contract — in `breakdown` JSONB so the document is immune to later contract
edits/deletes.

CRUD-ish over consum.invoices (see app/db/migrations/002_invoices.sql). The DB
enforces one closed invoice per customer per period (gist exclusion → 409);
invoices are voided, never deleted.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

import psycopg
from fastapi import HTTPException
from psycopg.types.json import Jsonb

from app.core import db
from app.services import consumption, pricing
from app.services import contracts as contracts_svc

logger = logging.getLogger("consum.invoices")

_TOTALS = ("energy_kwh", "energy_eur", "power_eur", "fixed_eur",
           "iee_eur", "vat_eur", "total_eur", "uncosted_kwh")


def _row_to_dict(row: Sequence[Any], with_breakdown: bool = True) -> Dict[str, Any]:
    keys = ("id", "customer_id", "period_start", "period_end", "status",
            "settlement", *_TOTALS, "breakdown", "created_by", "created_at",
            "voided_at", "voided_by", "origin", "pdf_filename")
    out: Dict[str, Any] = {}
    for k, v in zip(keys, row):
        if k in _TOTALS and v is not None:
            v = float(v)
        elif k in ("period_start", "period_end", "created_at", "voided_at") and v is not None:
            v = v.isoformat()
        elif k == "customer_id":
            v = str(v)
        out[k] = v
    if not with_breakdown:
        out.pop("breakdown", None)
    return out


_SELECT = ("SELECT id, customer_id, period_start, period_end, status, settlement, "
           + ", ".join(_TOTALS)
           + ", breakdown, created_by, created_at, voided_at, voided_by, "
           "origin, pdf_filename "
           "FROM consum.invoices")


async def list_for(slugs: Sequence[str]) -> List[Dict[str, Any]]:
    """Invoices for the authorized slugs (newest first, no breakdown blob)."""
    ids = await consumption._slugs_to_ids(slugs)
    if not ids:
        return []
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                _SELECT + " WHERE customer_id::text = ANY(%s) "
                "ORDER BY period_start DESC, id DESC",
                (list(ids.values()),),
            )
            return [_row_to_dict(r, with_breakdown=False) for r in await cur.fetchall()]


async def get(invoice_id: int) -> Optional[Dict[str, Any]]:
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(_SELECT + " WHERE id = %s", (invoice_id,))
            row = await cur.fetchone()
    return _row_to_dict(row) if row else None


def _contract_snapshot(contract: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A JSON-safe copy of the contract as it applied at close time."""
    if contract is None:
        return None
    return {k: v for k, v in contract.items() if k not in ("created_at", "updated_at")}


async def _pvpc_energy_only(kwh_by_hour: Dict[Tuple[str, int], float],
                            start: str, end: str) -> Dict[str, Any]:
    """Energy-only settlement for a gap with NO contract: PVPC energy by
    period + IEE + VAT at the legal defaults. No power/fixed terms (unknown
    without a contract) — flagged no_contract so the UI can say so."""
    pmap = pricing.hourly_rollup(await pricing.price_map([], start, end))
    energy = {p: {"kwh": 0.0, "eur": 0.0} for p in ("P1", "P2", "P3")}
    uncosted = 0.0
    for key, kwh in kwh_by_hour.items():
        row = pmap.get(key)
        if not row or row.get("price_eur_kwh") is None:
            uncosted += kwh
            continue
        p = row.get("period") if row.get("period") in energy else "P3"
        energy[p]["kwh"] += kwh
        energy[p]["eur"] += kwh * row["price_eur_kwh"]
    energy_eur = sum(v["eur"] for v in energy.values())
    energy_kwh = sum(v["kwh"] for v in energy.values())
    iee = energy_eur * 5.11269 / 100
    vat = (energy_eur + iee) * 21.0 / 100
    return {
        "start": start, "end": end,
        "days": (date.fromisoformat(end) - date.fromisoformat(start)).days + 1,
        "contract_id": None, "contract_type": "pvpc", "no_contract": True,
        "energy": {p: {"kwh": round(v["kwh"], 2), "eur": round(v["eur"], 2)}
                   for p, v in energy.items()},
        "energy_kwh": round(energy_kwh, 2), "energy_eur": round(energy_eur, 2),
        "uncosted_kwh": round(uncosted, 2),
        "power_eur": 0.0, "fixed_eur": 0.0,
        "iee_eur": round(iee, 2), "vat_eur": round(vat, 2),
        "total_eur": round(energy_eur + iee + vat, 2),
        "avg_eur_kwh": round((energy_eur + iee + vat) / energy_kwh, 5) if energy_kwh else None,
    }


def _segment_days(contracts: List[Dict[str, Any]], start: str, end: str
                  ) -> List[Tuple[str, str, Optional[Dict[str, Any]]]]:
    """Partition [start, end] into maximal day-runs each owned by one contract
    (or None). Returns [(seg_start, seg_end, contract_or_None), …] in order."""
    def covering(ds: str) -> Optional[Dict[str, Any]]:
        for c in contracts:
            if c["start_date"] <= ds and (c["end_date"] is None or c["end_date"] >= ds):
                return c
        return None

    segments: List[Tuple[str, str, Optional[Dict[str, Any]]]] = []
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    d = d0
    seg_start = d
    seg_c = covering(d.isoformat())
    while d <= d1:
        c = covering(d.isoformat())
        cid = c["id"] if c else None
        seg_cid = seg_c["id"] if seg_c else None
        if cid != seg_cid:
            segments.append((seg_start.isoformat(), (d - timedelta(days=1)).isoformat(), seg_c))
            seg_start, seg_c = d, c
        d += timedelta(days=1)
    segments.append((seg_start.isoformat(), d1.isoformat(), seg_c))
    return segments


async def close_period(customer_id: str, slugs: Sequence[str], start: str,
                       end: str, created_by: Optional[str]) -> Dict[str, Any]:
    """Freeze a settlement for [start, end] and persist it. Raises 409 if the
    period overlaps an existing closed invoice."""
    contracts = await contracts_svc.contracts_covering(customer_id, start, end)
    hourly = await consumption.energy_series(slugs, start, end, bucket="hour")
    kwh_all: Dict[Tuple[str, int], float] = {}
    for r in hourly:
        d = r["ts"][:10]
        if d < start or d > end:
            continue
        kwh_all[(d, int(r["ts"][11:13]))] = r["kwh"]

    segments: List[Dict[str, Any]] = []
    for seg_start, seg_end, contract in _segment_days(contracts, start, end):
        seg_kwh = {k: v for k, v in kwh_all.items() if seg_start <= k[0] <= seg_end}
        if contract and contract["contract_type"] != "pvpc":
            bd = await pricing.bill_breakdown(contract, seg_kwh, seg_start, seg_end)
        else:
            bd = await _pvpc_energy_only(seg_kwh, seg_start, seg_end)
        bd["contract"] = _contract_snapshot(contract)
        segments.append(bd)

    totals = {k: 0.0 for k in _TOTALS}
    for s in segments:
        totals["energy_kwh"] += s.get("energy_kwh", 0.0)
        totals["energy_eur"] += s.get("energy_eur", 0.0)
        totals["power_eur"] += s.get("power_eur", 0.0)
        totals["fixed_eur"] += s.get("fixed_eur", 0.0)
        totals["iee_eur"] += s.get("iee_eur", 0.0)
        totals["vat_eur"] += s.get("vat_eur", 0.0)
        totals["total_eur"] += s.get("total_eur", 0.0)
        totals["uncosted_kwh"] += s.get("uncosted_kwh", 0.0)
    totals = {k: round(v, 3 if k.endswith("kwh") else 2) for k, v in totals.items()}

    breakdown = {"period_start": start, "period_end": end, "settlement": "hourly",
                 "segments": segments, "totals": totals,
                 "generated_at": datetime.now().isoformat()}
    try:
        async with db.raw_connection() as con:
            async with con.cursor() as cur:
                await cur.execute(
                    "INSERT INTO consum.invoices (customer_id, period_start, period_end, "
                    "settlement, " + ", ".join(_TOTALS) + ", breakdown, created_by) "
                    "VALUES (%s, %s, %s, 'hourly', "
                    + ", ".join(["%s"] * len(_TOTALS)) + ", %s, %s) RETURNING id",
                    [customer_id, start, end, *[totals[k] for k in _TOTALS],
                     Jsonb(breakdown), created_by],
                )
                new_id = (await cur.fetchone())[0]
    except psycopg.errors.ExclusionViolation:
        raise HTTPException(409, detail="Ese periodo ya está facturado")
    logger.info("invoice %s closed for %s (%s..%s) by %s", new_id, customer_id,
                start, end, created_by)
    return await get(new_id)


async def void(invoice_id: int, voided_by: Optional[str]) -> Dict[str, Any]:
    """Void a closed invoice (frees its period for re-close). Never deletes."""
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                "UPDATE consum.invoices SET status='void', voided_at=now(), "
                "voided_by=%s WHERE id=%s AND status='closed' RETURNING id",
                (voided_by, invoice_id),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(404, detail="Factura no encontrada o ya anulada")
    return await get(invoice_id)


async def store_uploaded(customer_slug: str, pdf_bytes: bytes, filename: str,
                         extracted: Optional[Dict[str, Any]],
                         created_by: Optional[str],
                         markdown: Optional[str] = None) -> Dict[str, Any]:
    """Archive a REAL retailer invoice uploaded by the user (origin='uploaded',
    status='uploaded' — outside the closed-period exclusion). Stores the PDF
    inline (pilot scale) + the AI extraction as `breakdown` for the detail
    view. Period/total come from the extraction when present; a missing period
    falls back to the upload date so the row is still listable."""
    ids = await consumption._slugs_to_ids([customer_slug])
    cid = ids.get(customer_slug)
    if not cid:
        raise HTTPException(404, detail=f"Hogar desconocido: {customer_slug}")
    ex = extracted or {}

    def _d(key: str) -> Optional[str]:
        v = ex.get(key)
        if isinstance(v, str) and len(v) == 10:
            try:
                date.fromisoformat(v)
                return v
            except ValueError:
                return None
        return None

    today = date.today().isoformat()
    start = _d("billing_period_start") or today
    end = _d("billing_period_end") or start
    if end < start:
        start, end = end, start
    total = ex.get("total_eur")
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                """INSERT INTO consum.invoices
                     (customer_id, period_start, period_end, status, origin,
                      total_eur, breakdown, pdf, pdf_filename, created_by)
                   VALUES (%s, %s, %s, 'uploaded', 'uploaded', %s, %s, %s, %s, %s)
                   RETURNING id""",
                (cid, start, end, total if isinstance(total, (int, float)) else 0,
                 Jsonb({"uploaded": True, "extracted": ex,
                        "markdown": (markdown or "")[:60000] or None}),
                 pdf_bytes, filename, created_by),
            )
            new_id = (await cur.fetchone())[0]
    logger.info("uploaded invoice %s stored for %s (%s, %s bytes)",
                new_id, customer_slug, filename, len(pdf_bytes))
    return await get(new_id)


async def get_pdf(invoice_id: int) -> Optional[Tuple[bytes, str]]:
    """The stored PDF of an UPLOADED invoice (None for generated ones)."""
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                "SELECT pdf, pdf_filename FROM consum.invoices "
                "WHERE id = %s AND pdf IS NOT NULL", (invoice_id,))
            row = await cur.fetchone()
    return (bytes(row[0]), row[1] or "factura.pdf") if row else None


async def previous_closed(customer_id: str, before_start: str) -> Optional[Dict[str, Any]]:
    """The most recent CLOSED invoice starting before `before_start` for the
    same customer — feeds the plain-language comparison in /explain."""
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                _SELECT + " WHERE customer_id = %s AND status = 'closed' "
                "AND period_start < %s ORDER BY period_start DESC LIMIT 1",
                (customer_id, before_start))
            row = await cur.fetchone()
    return _row_to_dict(row) if row else None


async def set_uploaded_markdown(invoice_id: int, markdown: str) -> None:
    """Lazy backfill: persist the converted markdown of an uploaded invoice
    (rows archived before md persistence existed). Merges into breakdown."""
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                "UPDATE consum.invoices SET breakdown = breakdown || %s "
                "WHERE id = %s AND origin = 'uploaded'",
                (Jsonb({"markdown": markdown[:60000]}), invoice_id))


async def history_before(customer_id: str, before_start: str,
                         limit: int = 6) -> List[Dict[str, Any]]:
    """Recent invoices (closed AND uploaded, newest first) starting before
    `before_start` — the deterministic baseline for anomaly detection."""
    async with db.raw_connection() as con:
        async with con.cursor() as cur:
            await cur.execute(
                _SELECT + " WHERE customer_id = %s AND status != 'void' "
                "AND period_start < %s ORDER BY period_start DESC LIMIT %s",
                (customer_id, before_start, limit))
            return [_row_to_dict(r, with_breakdown=False) for r in await cur.fetchall()]
