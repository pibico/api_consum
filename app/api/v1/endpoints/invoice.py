"""Invoice endpoints (facturas) — CONSUM-IA business domain, phase 2.

Closing a period freezes a settlement (services/invoices.close_period); the
document is immutable thereafter (void, never edit). Reads are org-scoped;
closing needs role editor+, void needs admin+, the PDF is a PRO feature.
"""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from datetime import date as _date
from typing import Optional

import json

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query,
                     UploadFile)
from pydantic import BaseModel, model_validator

from app.api.v1.dependencies.rbac import (ConsumContext, consum_context,
                                          require_ai, require_role, require_tier)
from app.api.v1.endpoints.consumption import _slugs, _sp
from app.services import contracts, invoices

router = APIRouter(prefix="/invoices", tags=["invoices"])


class _LRUCache(OrderedDict):
    """Bounded LRU for invoice-derived results. Invoices are immutable once
    stored, so evicting an old entry only triggers a harmless recompute with
    identical output — this just caps unbounded process-lifetime growth."""
    _CAP = 512

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.move_to_end(key)
        if len(self) > self._CAP:
            self.popitem(last=False)


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
                        supply: Optional[int] = Query(None),
                        ctx: ConsumContext = Depends(consum_context)):
    """Invoices in the caller's scope (newest first, no breakdown blob).
    `supply` (F3) filtra por el CUPS del punto; sin CUPS asignado devuelve
    todas (no hay forma de atribuirlas todavía)."""
    slugs = await _slugs(ctx, customer)
    sp = await _sp(slugs, supply)
    if sp is not None and not sp.get("cups"):
        # Punto sin CUPS asignado: NINGUNA factura es atribuible a él —
        # devolver todas confundiría (visto en el piloto oficina 14-07).
        return {"values": []}
    return {"values": await invoices.list_for(slugs,
                                              cups=(sp or {}).get("cups"))}


@router.get("/billing-period")
async def billing_period(customer: Optional[str] = Query(None),
                         supply: Optional[int] = Query(None),
                         ctx: ConsumContext = Depends(require_tier("pro"))):
    """PRO — the OPEN billing period (anchored on the invoice history) costed
    live + projection to the expected close. The 'factura en curso' KPI."""
    slugs = await _slugs(ctx, customer, min_tier="pro")
    if not slugs:
        raise HTTPException(400, detail="no household in scope")
    return await invoices.billing_period_live(slugs[0],
                                              sp=await _sp(slugs, supply))


class NextCloseIn(BaseModel):
    date: Optional[_date] = None    # null → back to the median estimate


@router.put("/billing-period/expected-end")
async def set_expected_end(body: NextCloseIn,
                           customer: Optional[str] = Query(None),
                           ctx: ConsumContext = Depends(consum_context)):
    """Self-service: the household sets WHEN its open period closes (their
    meter-reading day) — beats the median estimate in the KPI. Null clears."""
    if ctx.is_service and not ctx.is_superadmin:
        raise HTTPException(403, detail="read-only service key")
    slugs = await _slugs(ctx, customer)
    if not slugs:
        raise HTTPException(400, detail="no household in scope")
    slug = ctx.check_slug(slugs[0])
    if body.date is not None and body.date < _date.today():
        raise HTTPException(422, detail="La fecha de cierre no puede ser pasada.")
    return await invoices.set_next_close(slug, body.date.isoformat() if body.date else None,
                                         (ctx.user or {}).get("email"))


@router.get("/{invoice_id}")
async def get_invoice(invoice_id: int,
                      ctx: ConsumContext = Depends(consum_context)):
    """One invoice with its full frozen breakdown."""
    inv = await invoices.get(invoice_id)
    if not inv:
        raise HTTPException(404, detail="Factura no encontrada")
    await _check_invoice_scope(ctx, inv)
    return inv


# NOTE: declared before /{invoice_id} routes would matter only for GET; POST
# paths don't clash. Self-service: any household member archives their bill.
_MAX_UPLOAD = 8 * 1024 * 1024


@router.post("/upload", status_code=201)
async def upload_invoice(
        file: UploadFile = File(...),
        customer: Optional[str] = Form(None),
        extracted: Optional[str] = Form(None),   # JSON of the AI extraction
        markdown: Optional[str] = Form(None),    # full converted text (explain)
        ctx: ConsumContext = Depends(consum_context)):
    """Archive the user's REAL retailer bill (the Facturas upload flow keeps
    the document, besides configuring the tariff). Self-service."""
    if ctx.is_service and not ctx.is_superadmin:
        raise HTTPException(403, detail="read-only service key")
    slugs = await _slugs(ctx, customer)
    if not slugs:
        raise HTTPException(400, detail="no household in scope")
    slug = slugs[0]
    ctx.check_slug(slug)
    blob = await file.read()
    if len(blob) > _MAX_UPLOAD:
        raise HTTPException(413, detail="El archivo es demasiado grande (máx. 8 MB).")
    ex = None
    if extracted:
        try:
            ex = json.loads(extracted)
        except ValueError:
            ex = None
    inv = await invoices.store_uploaded(slug, blob, file.filename or "factura.pdf",
                                        ex, (ctx.user or {}).get("email"),
                                        markdown=markdown)
    # Per-band kWh (P1/P2/P3) live in the bill TEXT, not in the tariff
    # extraction — fill the mig-008 columns in the background (one amounts
    # call) so the list shows the split without delaying the upload response.
    if markdown:
        asyncio.get_running_loop().create_task(_fill_band_kwh(inv["id"], markdown))
    return inv


async def _fill_band_kwh(invoice_id: int, markdown: str) -> None:
    """Best-effort: AI amounts extraction → energy_p1/2/3_kwh columns."""
    try:
        from app.services.ai import registry
        got = await registry.get("invoice").extract_bill_amounts(markdown)
        if got and (got.kwh_horas_caras or got.kwh_horas_normales or got.kwh_horas_baratas):
            await invoices.set_band_kwh(invoice_id, got.kwh_horas_caras,
                                        got.kwh_horas_normales, got.kwh_horas_baratas)
    except Exception:                                    # noqa: BLE001
        pass                                             # list shows — until then


@router.get("/{invoice_id}/file")
async def uploaded_file(invoice_id: int,
                        ctx: ConsumContext = Depends(consum_context)):
    """Download the stored PDF of an UPLOADED invoice (owner-scoped)."""
    from fastapi.responses import Response
    inv = await invoices.get(invoice_id)
    if not inv:
        raise HTTPException(404, detail="Factura no encontrada")
    await _check_invoice_scope(ctx, inv)
    got = await invoices.get_pdf(invoice_id)
    if not got:
        raise HTTPException(404, detail="Esta factura no tiene documento adjunto")
    data, fname = got
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


@router.post("/close", status_code=201)
async def close(body: CloseIn,
                ctx: ConsumContext = Depends(require_role("editor"))):
    """Freeze a settlement for [start, end] (editor+). 409 if it overlaps an
    existing closed invoice."""
    ctx.check_slug(body.customer, min_role="editor")
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
    await _check_invoice_scope(ctx, inv, min_role="admin")
    return await invoices.void(invoice_id, (ctx.user or {}).get("email"))


@router.delete("/{invoice_id}")
async def delete_uploaded(invoice_id: int,
                          ctx: ConsumContext = Depends(consum_context)):
    """Delete a WRONGLY UPLOADED bill (status='uploaded' only). Self-service,
    symmetric with /upload: any household member can remove their own bad
    upload; closed statements still go through /void (admin+)."""
    if ctx.is_service and not ctx.is_superadmin:
        raise HTTPException(403, detail="read-only service key")
    inv = await invoices.get(invoice_id)
    if not inv:
        raise HTTPException(404, detail="Factura no encontrada")
    await _check_invoice_scope(ctx, inv)
    await invoices.delete_uploaded(invoice_id)
    return {"status": "deleted", "id": invoice_id}


@router.get("/{invoice_id}/pdf")
async def pdf(invoice_id: int,
              ctx: ConsumContext = Depends(require_tier("pro"))):
    """PRO — the invoice as a branded PDF."""
    from fastapi.responses import Response

    from app.services import invoice_pdf
    inv = await invoices.get(invoice_id)
    if not inv:
        raise HTTPException(404, detail="Factura no encontrada")
    await _check_invoice_scope(ctx, inv, min_tier="pro")
    # WeasyPrint is CPU-bound (~1-2 s) — keep it off the event loop.
    data = await asyncio.to_thread(invoice_pdf.render, inv)
    fname = f"factura_{inv['period_start']}_{inv['period_end']}.pdf"
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": f"attachment; filename={fname}"})


# "Tu factura, explicada" — InvoiceSkill (phase 2). An invoice is immutable
# once stored, so the explanation is computed ONCE per process and cached.
_explain_cache: _LRUCache = _LRUCache()


@router.get("/{invoice_id}/amounts")
async def amounts(invoice_id: int,
                  ctx: ConsumContext = Depends(consum_context)):
    """Structured cost concepts of ONE invoice (the colored breakdown panel).
    Generated rows read their own columns (free); uploaded rows reuse the
    cached AI amounts extraction — may be null when AI is off."""
    inv = await invoices.get(invoice_id)
    if not inv:
        raise HTTPException(404, detail="Factura no encontrada")
    await _check_invoice_scope(ctx, inv)
    if inv.get("origin") == "uploaded":
        inv = await _ensure_uploaded_markdown(inv)
    got = await _compute_amounts(inv)
    return {"amounts": got, "total_eur": inv.get("total_eur"),
            "energy_kwh": inv.get("energy_kwh"),
            "period_start": inv.get("period_start"),
            "period_end": inv.get("period_end")}


@router.get("/{invoice_id}/explain")
async def explain(invoice_id: int, regenerate: bool = False,
                  ctx: ConsumContext = Depends(require_ai())):
    """Plain-language explanation of an invoice (generated or uploaded), with
    a comparison against the previous closed one when it exists. Computed ONCE
    and PERSISTED with the invoice; `?regenerate=1` (the panel's 'Regenerar')
    forces a fresh take when the first one doesn't convince."""
    inv = await invoices.get(invoice_id)
    if not inv:
        raise HTTPException(404, detail="Factura no encontrada")
    await _check_invoice_scope(ctx, inv)
    if not regenerate and invoice_id in _explain_cache:
        return {"explanation": _explain_cache[invoice_id], "cached": True}
    text = await _compute_explanation(inv, regenerate=regenerate)
    return {"explanation": text, "cached": False}


class AskIn(BaseModel):
    question: str

    @model_validator(mode="after")
    def _check(self):
        q = (self.question or "").strip()
        if not (3 <= len(q) <= 300):
            raise ValueError("la pregunta debe tener entre 3 y 300 caracteres")
        self.question = q
        return self


@router.post("/{invoice_id}/ask")
async def ask(invoice_id: int, body: AskIn,
              ctx: ConsumContext = Depends(require_ai())):
    """Plain-language Q&A about ONE invoice — grounded on its own facts and
    stored bill text, same footing as /explain. Not cached (questions vary)."""
    inv = await invoices.get(invoice_id)
    if not inv:
        raise HTTPException(404, detail="Factura no encontrada")
    await _check_invoice_scope(ctx, inv)
    from app.services.ai import registry
    from app.services.ai.invoices import facts_for_invoice
    inv = await _ensure_uploaded_markdown(inv)
    prev = await invoices.previous_closed(inv["customer_id"], inv["period_start"])
    if prev and prev.get("id") == invoice_id:
        prev = None
    facts = facts_for_invoice(inv, prev)
    answer = await registry.get("invoice").answer_billing_question(body.question, facts)
    if not answer:
        raise HTTPException(503, detail="La IA no está disponible ahora mismo.")
    return {"answer": answer}


async def _compute_explanation(inv: dict, regenerate: bool = False) -> str:
    """Explanation resolution: in-proc cache → PERSISTED in the invoice's
    breakdown (survives restarts — never re-bill the AI for work already done)
    → LLM, persisting afterwards. `regenerate` skips both and overwrites."""
    from app.services.ai import registry
    from app.services.ai.invoices import facts_for_invoice

    iid = inv["id"]
    if not regenerate:
        if iid in _explain_cache:
            return _explain_cache[iid]
        saved = ((inv.get("breakdown") or {}).get("ai_explanation") or {}).get("text")
        if saved:
            _explain_cache[iid] = saved
            return saved
    inv = await _ensure_uploaded_markdown(inv)
    prev = await invoices.previous_closed(inv["customer_id"], inv["period_start"])
    if prev and prev.get("id") == iid:
        prev = None
    facts = facts_for_invoice(inv, prev)
    text = await registry.get("invoice").explain_invoice_plain(facts)
    if not text:
        raise HTTPException(503, detail="La explicación con IA no está disponible ahora.")
    _explain_cache[iid] = text
    await invoices.set_breakdown_key(iid, "ai_explanation",
                                     {"text": text, "at": _date.today().isoformat()})
    return text


async def _ensure_uploaded_markdown(inv: dict) -> dict:
    """Lazy backfill: uploaded rows archived before md persistence get their
    stored PDF converted once (api_convert) and the text merged into breakdown
    — so 'Explicar' can use the real line items on old uploads too."""
    bd = inv.get("breakdown") or {}
    if inv.get("origin") != "uploaded" or bd.get("markdown"):
        return inv
    from app.services import convert_client
    got = await invoices.get_pdf(inv["id"])
    if not got:
        return inv
    data, fname = got
    md = await convert_client.to_markdown(data, fname, "application/pdf")
    if md:
        await invoices.set_uploaded_markdown(inv["id"], md)
        bd["markdown"] = md
        inv["breakdown"] = bd
    return inv


# detect_cost_anomaly — the VERDICT is deterministic (computed €/día and
# kWh/día deviations vs the household's own history); the LLM only phrases it
# in plain language. Degrades to a template sentence when AI is off.
_anomaly_cache: _LRUCache = _LRUCache()
_ANOM_WARN = 0.25    # |desviación €/día| ≥ 25 % → aviso
_ANOM_ALERT = 0.50   # ≥ 50 % → alerta


def _per_day(inv: dict) -> Optional[dict]:
    from datetime import date as _d
    try:
        days = (_d.fromisoformat(inv["period_end"])
                - _d.fromisoformat(inv["period_start"])).days + 1
    except (TypeError, ValueError):
        return None
    if days <= 0 or not inv.get("total_eur"):
        return None
    out = {"days": days, "eur_day": inv["total_eur"] / days}
    if inv.get("energy_kwh"):
        out["kwh_day"] = inv["energy_kwh"] / days
    return out


@router.get("/{invoice_id}/anomaly")
async def anomaly(invoice_id: int,
                  ctx: ConsumContext = Depends(consum_context)):
    """Is this invoice unusually expensive vs the household's own history?
    Deterministic verdict + (if AI enabled) plain-language narrative."""
    inv = await invoices.get(invoice_id)
    if not inv:
        raise HTTPException(404, detail="Factura no encontrada")
    await _check_invoice_scope(ctx, inv)
    if invoice_id in _anomaly_cache:
        return {**_anomaly_cache[invoice_id], "cached": True}
    out = await _compute_anomaly(ctx.ai_enabled, inv)
    return {**out, "cached": False}


async def _compute_anomaly(ai_enabled: bool, inv: dict) -> dict:
    """Deterministic anomaly verdict (+ optional LLM narrative), cached."""
    from app.services.ai import registry

    invoice_id = inv["id"]
    if invoice_id in _anomaly_cache:
        return _anomaly_cache[invoice_id]
    cur = _per_day(inv)
    hist = await invoices.history_before(inv["customer_id"], inv["period_start"])
    hist_pd = [p for p in (_per_day(h) for h in hist) if p]
    if not cur or len(hist_pd) < 1:
        out = {"status": "no_baseline", "level": "normal", "is_anomaly": False,
               "message": "Aún no hay histórico suficiente para comparar."}
        _anomaly_cache[invoice_id] = out
        return out

    base_eur = sum(p["eur_day"] for p in hist_pd) / len(hist_pd)
    dev_eur = (cur["eur_day"] - base_eur) / base_eur if base_eur else 0.0
    kwh_devs = [p["kwh_day"] for p in hist_pd if p.get("kwh_day")]
    dev_kwh = None
    if cur.get("kwh_day") and kwh_devs:
        base_kwh = sum(kwh_devs) / len(kwh_devs)
        dev_kwh = (cur["kwh_day"] - base_kwh) / base_kwh if base_kwh else None
    level = ("alerta" if abs(dev_eur) >= _ANOM_ALERT
             else "aviso" if abs(dev_eur) >= _ANOM_WARN else "normal")

    signals = {
        "veredicto_calculado": level,
        "gasto_por_dia_esta_factura_eur": round(cur["eur_day"], 2),
        "gasto_por_dia_habitual_eur": round(base_eur, 2),
        "desviacion_gasto_pct": round(dev_eur * 100, 1),
        "desviacion_consumo_kwh_pct": round(dev_kwh * 100, 1) if dev_kwh is not None else None,
        "facturas_en_el_historico": len(hist_pd),
        "dias_del_periodo": cur["days"],
    }
    out = {"status": "ok", "level": level, "is_anomaly": level != "normal",
           "signals": signals}
    narrative = None
    if ai_enabled:
        n = await registry.get("invoice").narrate_anomaly(signals)
        if n:
            narrative = n.model_dump()
    if not narrative:
        pct = f"{abs(round(dev_eur * 100))} %"
        narrative = {"headline": (
            f"Esta factura sale a {signals['gasto_por_dia_esta_factura_eur']} €/día, "
            + (f"un {pct} más que" if dev_eur > 0 else f"un {pct} menos que"
               if dev_eur < 0 else "igual que")
            + f" tu media habitual ({signals['gasto_por_dia_habitual_eur']} €/día)."),
            "causes": [], "advice": None}
    out["narrative"] = narrative
    _anomaly_cache[invoice_id] = out
    return out


_amounts_cache: _LRUCache = _LRUCache()


async def _compute_amounts(inv: dict) -> Optional[dict]:
    """Structured line-item amounts for the visuals. Generated invoices carry
    them as columns/segments (free); uploaded bills get a deterministic LLM
    extraction from the stored markdown (cached — the bill is immutable)."""
    iid = inv["id"]
    if iid in _amounts_cache:
        return _amounts_cache[iid]
    out: Optional[dict] = None
    if inv.get("origin") != "uploaded":
        per = {"P1": 0.0, "P2": 0.0, "P3": 0.0}
        for s in (inv.get("breakdown") or {}).get("segments") or []:
            for p, v in (s.get("energy") or {}).items():
                if p in per and isinstance(v, dict):
                    per[p] += v.get("kwh") or 0
        out = {"energia_eur": inv.get("energy_eur"),
               "potencia_eur": inv.get("power_eur"),
               "alquiler_eur": inv.get("fixed_eur"),
               "impuestos_eur": round((inv.get("iee_eur") or 0) + (inv.get("vat_eur") or 0), 2),
               "kwh_horas_caras": per["P1"] or None,
               "kwh_horas_normales": per["P2"] or None,
               "kwh_horas_baratas": per["P3"] or None}
    else:
        from app.services.ai import registry
        bd = inv.get("breakdown") or {}
        saved = bd.get("ai_amounts")
        if saved:                       # persisted — never re-bill the AI
            _amounts_cache[iid] = saved
            return saved
        md = bd.get("markdown")
        if md:
            got = await registry.get("invoice").extract_bill_amounts(md)
            if got:
                out = got.model_dump()
                await invoices.set_breakdown_key(iid, "ai_amounts", out)
                # Lazy backfill of the mig-008 band columns for rows uploaded
                # before the background fill existed.
                if inv.get("energy_p1_kwh") is None and \
                   (got.kwh_horas_caras or got.kwh_horas_normales or got.kwh_horas_baratas):
                    await invoices.set_band_kwh(iid, got.kwh_horas_caras,
                                                got.kwh_horas_normales,
                                                got.kwh_horas_baratas)
    if out is not None:
        _amounts_cache[iid] = out
    return out


@router.get("/{invoice_id}/explain/pdf")
async def explain_pdf_route(invoice_id: int,
                            ctx: ConsumContext = Depends(require_ai())):
    """The plain-language explanation as a branded, printable one-pager
    (colorful highlights, cost bar, per-band cards + anomaly band). Reuses the
    cached explanation and anomaly; WeasyPrint runs off the event loop."""
    from fastapi.responses import Response

    from app.services import explain_pdf
    inv = await invoices.get(invoice_id)
    if not inv:
        raise HTTPException(404, detail="Factura no encontrada")
    await _check_invoice_scope(ctx, inv)
    text = await _compute_explanation(inv)
    anom = await _compute_anomaly(ctx.ai_enabled, inv)
    inv = await invoices.get(invoice_id)   # re-read: markdown may have backfilled
    amounts = await _compute_amounts(inv)
    data = await asyncio.to_thread(explain_pdf.render, inv, text, anom, amounts)
    fname = f"factura_explicada_{inv['period_start']}_{inv['period_end']}.pdf"
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": f"inline; filename={fname}"})


async def _check_invoice_scope(ctx: ConsumContext, inv: dict, *,
                               min_role: Optional[str] = None,
                               min_tier: Optional[str] = None) -> None:
    """A superadmin/service passes; a member must own the invoice's household.
    The invoice stores customer_id — resolve it to a slug the context knows,
    then enforce any per-org role/tier requirement on THAT slug (not the caller's
    flattened global max)."""
    if ctx.is_superadmin or ctx.is_service:
        return
    from app.services import consumption
    slugs = sorted(ctx.customer_slugs)
    ids = await consumption._slugs_to_ids(slugs)
    owner = next((s for s, i in ids.items() if i == inv["customer_id"]), None)
    if owner is None:
        raise HTTPException(403, detail="Sin acceso a esa factura")
    ctx.check_slug(owner, min_role=min_role, min_tier=min_tier)
