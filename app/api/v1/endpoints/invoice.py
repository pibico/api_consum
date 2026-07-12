"""Invoice endpoints (facturas) — CONSUM-IA business domain, phase 2.

Closing a period freezes a settlement (services/invoices.close_period); the
document is immutable thereafter (void, never edit). Reads are org-scoped;
closing needs role editor+, void needs admin+, the PDF is a PRO feature.
"""
from __future__ import annotations

import asyncio
from datetime import date as _date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, model_validator

from app.api.v1.dependencies.rbac import (ConsumContext, consum_context,
                                          require_role, require_tier)
from app.api.v1.endpoints.consumption import _slugs
from app.services import contracts, invoices

router = APIRouter(prefix="/invoices", tags=["invoices"])


class CloseIn(BaseModel):
    customer: str
    start: _date
    end: _date

    @model_validator(mode="after")
    def _check(self):
        if self.end < self.start:
            raise ValueError("end debe ser >= start")
        if self.end >= _date.today():
            raise ValueError("solo se pueden facturar periodos ya cerrados (end < hoy)")
        return self


@router.get("")
async def list_invoices(customer: Optional[str] = Query(None),
                        ctx: ConsumContext = Depends(consum_context)):
    """Invoices in the caller's scope (newest first, no breakdown blob)."""
    slugs = await _slugs(ctx, customer)
    return {"values": await invoices.list_for(slugs)}


@router.get("/{invoice_id}")
async def get_invoice(invoice_id: int,
                      ctx: ConsumContext = Depends(consum_context)):
    """One invoice with its full frozen breakdown."""
    inv = await invoices.get(invoice_id)
    if not inv:
        raise HTTPException(404, detail="Factura no encontrada")
    await _check_invoice_scope(ctx, inv)
    return inv


@router.post("/close", status_code=201)
async def close(body: CloseIn,
                ctx: ConsumContext = Depends(require_role("editor"))):
    """Freeze a settlement for [start, end] (editor+). 409 if it overlaps an
    existing closed invoice."""
    ctx.check_slug(body.customer)
    start, end = body.start.isoformat(), body.end.isoformat()
    cid, _ = await contracts.resolve_for_slugs([body.customer], start, end)
    if not cid:
        raise HTTPException(400, detail="No se pudo resolver el hogar para facturar")
    return await invoices.close_period(cid, [body.customer], start, end,
                                       (ctx.user or {}).get("email"))


@router.post("/{invoice_id}/void")
async def void(invoice_id: int,
               ctx: ConsumContext = Depends(require_role("admin"))):
    """Void a closed invoice (admin+) — frees its period for re-close."""
    inv = await invoices.get(invoice_id)
    if not inv:
        raise HTTPException(404, detail="Factura no encontrada")
    await _check_invoice_scope(ctx, inv)
    return await invoices.void(invoice_id, (ctx.user or {}).get("email"))


@router.get("/{invoice_id}/pdf")
async def pdf(invoice_id: int,
              ctx: ConsumContext = Depends(require_tier("pro"))):
    """PRO — the invoice as a branded PDF."""
    from fastapi.responses import Response

    from app.services import invoice_pdf
    inv = await invoices.get(invoice_id)
    if not inv:
        raise HTTPException(404, detail="Factura no encontrada")
    await _check_invoice_scope(ctx, inv)
    # WeasyPrint is CPU-bound (~1-2 s) — keep it off the event loop.
    data = await asyncio.to_thread(invoice_pdf.render, inv)
    fname = f"factura_{inv['period_start']}_{inv['period_end']}.pdf"
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


async def _check_invoice_scope(ctx: ConsumContext, inv: dict) -> None:
    """A superadmin/service passes; a member must own the invoice's household.
    The invoice stores customer_id — resolve it to a slug the context knows."""
    if ctx.is_superadmin or ctx.is_service:
        return
    from app.services import consumption
    slugs = sorted(ctx.customer_slugs)
    ids = await consumption._slugs_to_ids(slugs)
    if inv["customer_id"] not in set(ids.values()):
        raise HTTPException(403, detail="Sin acceso a esa factura")
