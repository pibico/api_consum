"""Supply-contract endpoints (entidad contrato) — api_consum business domain.

Reads follow the usual org-scoped `_slugs` pattern; writes need role editor+
of the owning org (superadmin any household; plain service key read-only).
The active contract drives ALL €-costing via services/pricing.py — households
without one keep costing at PVPC exactly as before.
"""
from __future__ import annotations

import re
from datetime import date as _date
from typing import Any, Dict, Optional

from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query,
                     UploadFile)
from pydantic import BaseModel, Field, field_validator, model_validator

from app.api.v1.dependencies.rbac import (ConsumContext, consum_context,
                                          require_role, require_tier)
from app.api.v1.endpoints.consumption import _slugs, _sp
from app.services import (ai_client, consumption, contracts, convert_client,
                          exo_client, invoices, pdf_text, pricing,
                          tariff_catalog)
from app.services.ai import registry

router = APIRouter(prefix="/contracts", tags=["contracts"])

_CUPS_RE = re.compile(r"^ES\d{16}[A-Z0-9]{0,4}$")


class ContractIn(BaseModel):
    customer: str
    contract_type: str = Field(..., pattern="^(pvpc|fixed|indexed)$")
    label: Optional[str] = None
    retailer: Optional[str] = None
    cups: Optional[str] = None
    access_tariff: str = "2.0TD"
    start_date: _date
    end_date: Optional[_date] = None
    energy_p1_eur_kwh: Optional[float] = Field(None, gt=0, lt=10)
    energy_p2_eur_kwh: Optional[float] = Field(None, gt=0, lt=10)
    energy_p3_eur_kwh: Optional[float] = Field(None, gt=0, lt=10)
    margin_eur_kwh: Optional[float] = Field(None, ge=0, lt=1)
    passthru_p1_eur_kwh: float = Field(0, ge=0, lt=1)
    passthru_p2_eur_kwh: float = Field(0, ge=0, lt=1)
    passthru_p3_eur_kwh: float = Field(0, ge=0, lt=1)
    power_p1_kw: Optional[float] = Field(None, gt=0, le=15)
    power_p2_kw: Optional[float] = Field(None, gt=0, le=15)
    power_p1_eur_kw_day: Optional[float] = Field(None, ge=0, lt=5)
    power_p2_eur_kw_day: Optional[float] = Field(None, ge=0, lt=5)
    meter_rental_eur_month: float = Field(0.81, ge=0, lt=20)
    other_fixed_eur_month: float = Field(0, ge=0, lt=100)
    electricity_tax_pct: float = Field(5.11269, ge=0, le=15)
    vat_pct: float = Field(21.0, ge=0, le=21)
    # Indexed price components (api_exo parity): {SA:{P1,P2,P3}, CC:{…}, …}.
    # When present it drives indexed costing via the full formula; margin/passthru
    # stay as the legacy fallback.
    components: Optional[Dict[str, Any]] = None
    notes: Optional[str] = None

    @field_validator("cups")
    @classmethod
    def _cups(cls, v):
        if v:
            v = v.strip().upper()
            if not _CUPS_RE.match(v):
                raise ValueError("CUPS inválido (formato ES + 16 dígitos + control)")
        return v or None

    @field_validator("access_tariff")
    @classmethod
    def _tariff(cls, v):
        if v != "2.0TD":
            raise ValueError("Solo tarifa de acceso 2.0TD en esta versión")
        return v

    @model_validator(mode="after")
    def _by_type(self):
        if self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date debe ser >= start_date")
        if self.contract_type == "fixed" and self.energy_p1_eur_kwh is None:
            raise ValueError("Contrato fijo: falta energy_p1_eur_kwh")
        if self.contract_type == "indexed" and self.margin_eur_kwh is None \
                and not self.components:
            raise ValueError("Contrato indexado: falta margin_eur_kwh o components")
        return self


class ContractUpdate(ContractIn):
    # PUT reuses the full model minus the immutable owner (slug comes from the
    # stored row); everything else is resent by the form.
    customer: Optional[str] = None
    contract_type: Optional[str] = Field(None, pattern="^(pvpc|fixed|indexed)$")
    start_date: Optional[_date] = None

    @model_validator(mode="after")
    def _by_type(self):
        if (self.end_date and self.start_date) and self.end_date < self.start_date:
            raise ValueError("end_date debe ser >= start_date")
        if self.contract_type == "fixed" and self.energy_p1_eur_kwh is None:
            raise ValueError("Contrato fijo: falta energy_p1_eur_kwh")
        if self.contract_type == "indexed" and self.margin_eur_kwh is None \
                and not self.components:
            raise ValueError("Contrato indexado: falta margin_eur_kwh o components")
        return self


@router.get("")
async def list_contracts(customer: Optional[str] = Query(None),
                         supply: Optional[int] = Query(None),
                         ctx: ConsumContext = Depends(consum_context)):
    """All contracts (history included) in the caller's scope. `supply`
    (F3) filtra los del punto (los legados sin punto solo salen en Todos)."""
    slugs = await _slugs(ctx, customer)
    values = await contracts.list_for(slugs)
    sp = await _sp(slugs, supply)
    if sp is not None:
        values = [v for v in values if v.get("supply_point_id") == sp["id"]]
    return {"values": values}


@router.get("/active")
async def active(customer: Optional[str] = Query(None),
                 date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
                 supply: Optional[int] = Query(None),
                 ctx: ConsumContext = Depends(consum_context)):
    """The contract covering `date` (default today) + the current-hour price.
    Feeds the Panel. Multi-household scopes → contract: null, fallback pvpc.
    `supply` (F3): el contrato de ESE punto (sin contrato → pvpc)."""
    slugs = await _slugs(ctx, customer)
    on = date or _date.today().isoformat()
    sp = await _sp(slugs, supply)
    sp_id = (sp or {}).get("id")
    cid, _ = await contracts.resolve_for_slugs(slugs, on, on, supply_point_id=sp_id)
    contract = await contracts.get_active(cid, on, supply_point_id=sp_id) if cid else None
    if sp is not None and contract is not None and contract.get("supply_point_id") != sp_id:
        contract = None   # el fallback legado NULL no aplica: el punto no tiene contrato
    return {
        "contract": contract,
        "fallback": None if contract else "pvpc",
        "price_now": await pricing.price_now(contract),
    }


@router.post("", status_code=201)
async def create(body: ContractIn,
                 ctx: ConsumContext = Depends(consum_context)):
    # Self-service: any authenticated household member manages their own supply
    # contract (decision 2026-07-12). check_slug still confines to own homes; a
    # read-only service key is refused. (DELETE stays admin.)
    if ctx.is_service and not ctx.is_superadmin:
        raise HTTPException(403, detail="read-only service key")
    ctx.check_slug(body.customer)
    payload = body.model_dump(exclude={"customer"})
    return await contracts.create(body.customer, payload,
                                  (ctx.user or {}).get("email"))


@router.put("/{contract_id}")
async def update(contract_id: int, body: ContractUpdate,
                 ctx: ConsumContext = Depends(consum_context)):
    if ctx.is_service and not ctx.is_superadmin:
        raise HTTPException(403, detail="read-only service key")
    row = await contracts.get(contract_id)
    if not row:
        raise HTTPException(404, detail="Contrato no encontrado")
    ctx.check_slug(row["slug"])
    payload = body.model_dump(exclude={"customer"}, exclude_unset=True)
    # end_date is legitimately settable to NULL (reactivate) — model_dump with
    # exclude_unset keeps it only when the form sent it.
    return await contracts.update(contract_id, payload)


@router.delete("/{contract_id}")
async def remove(contract_id: int,
                 ctx: ConsumContext = Depends(consum_context)):
    """Hard delete — self-service (2026-07-12): a household member must be able
    to remove an INCORRECT contract of their own home. check_slug confines to
    own households; read-only service keys are refused. The UI still favours
    end-dating for real history (feeds invoicing of past periods)."""
    if ctx.is_service and not ctx.is_superadmin:
        raise HTTPException(403, detail="read-only service key")
    row = await contracts.get(contract_id)
    if not row:
        raise HTTPException(404, detail="Contrato no encontrado")
    ctx.check_slug(row["slug"])
    await contracts.delete(contract_id)
    return {"deleted": contract_id}


@router.get("/bill-estimate")
async def bill_estimate(month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
                        customer: Optional[str] = Query(None),
                        ctx: ConsumContext = Depends(require_tier("pro"))):
    """PRO — full-bill estimate for one month under the active contract:
    energy by period + power term + fixed charges + IEE + IVA."""
    import calendar
    slugs = await _slugs(ctx, customer, min_tier="pro")
    y, m = int(month[:4]), int(month[5:7])
    last = calendar.monthrange(y, m)[1]
    start, end = f"{month}-01", f"{month}-{last:02d}"
    today = _date.today().isoformat()
    if end > today:
        end = today if today >= start else start
    cid, covering = await contracts.resolve_for_slugs(slugs, start, end)
    if not cid or not covering:
        return {"status": "no_contract", "month": month}
    # Bill under the contract active at range end (mid-month switches estimate
    # only the tail contract's terms — invoicing proper is phase 2).
    contract = covering[-1]
    hourly = await consumption.energy_series(slugs, start, end, bucket="hour")
    kwh_by_hour = {}
    for r in hourly:
        d, h = r["ts"][:10], int(r["ts"][11:13])
        if start <= d <= end:
            kwh_by_hour[(d, h)] = kwh_by_hour.get((d, h), 0.0) + r["kwh"]
    breakdown = await pricing.bill_breakdown(contract, kwh_by_hour, start, end)
    breakdown.update({"status": "ok", "month": month, "days_in_month": last})
    return breakdown


# ── AI extraction: the "place to give the contract" ──────────────────────────
# Upload the contract/tariff document (PDF/photo) or paste its text; the
# ContractSkill returns the fields for the user/operator to REVIEW before saving.
# Self-service (any authenticated household member). NEVER auto-saves.
_MAX_UPLOAD = 8 * 1024 * 1024   # 8 MB


@router.post("/extract")
async def extract_contract(
        file: Optional[UploadFile] = File(None),
        text: Optional[str] = Form(None),
        use_vlm: bool = Form(False),
        ctx: ConsumContext = Depends(consum_context)):
    """PDF/foto (→ api_convert) o texto pegado → campos del contrato extraídos
    por IA, para revisar antes de guardar. `use_vlm=true` para escaneos/fotos."""
    if not ai_client.configured():
        raise HTTPException(503, detail="La lectura con IA no está disponible ahora.")
    md: Optional[str] = None
    blob: Optional[bytes] = None      # kept for the raw-PDF CUPS fallback below
    fname: Optional[str] = None
    if file is not None:
        blob = await file.read()
        if len(blob) > _MAX_UPLOAD:
            raise HTTPException(413, detail="El archivo es demasiado grande (máx. 8 MB).")
        fname = file.filename or "contrato"
        ctype = file.content_type or "application/octet-stream"
        # Plain-text/markdown uploads ARE already text — skip api_convert.
        if ctype.startswith("text/") or fname.lower().endswith((".md", ".txt", ".markdown")):
            md = blob.decode("utf-8", errors="ignore").strip() or None
        else:
            md = await convert_client.to_markdown(blob, fname, ctype, use_vlm=use_vlm)
        if not md:
            raise HTTPException(502, detail="No se pudo leer el documento. Prueba con otra foto o pega el texto.")
    elif text and text.strip():
        md = text.strip()
    else:
        raise HTTPException(400, detail="Sube un archivo o pega el texto del contrato.")

    skill = registry.get("contract")
    row = await skill.extract_tariff_sheet(md)
    if row is None:
        raise HTTPException(503, detail="La IA no pudo interpretar el documento. Revísalo a mano.")
    # Sanity gate: an electricity document must carry at least one HARD energy
    # signal. `retailer`/`document_kind` alone don't count — the model happily
    # hallucinates both on unrelated docs (verified: a project memo came back
    # as document_kind='ficha', retailer='pibiCo', confidence 1.0).
    d = row.model_dump()
    # docling DESCARTA texto que sí está en la capa del PDF, y el CUPS cae ahí
    # con frecuencia (caso 03-08 en facturas). En esta ruta el daño es otro: el
    # contrato se guarda con CUPS vacío y ya no se puede casar con su punto de
    # suministro (contrato 19 / oficina, 05-08). Misma red de seguridad de tres
    # fuentes que services/invoices.store_uploaded: markdown → capa de texto
    # del PDF (cuesta CPU: solo si hace falta) → nombre del fichero.
    if not d.get("cups"):
        for source in (lambda: md,
                       lambda: pdf_text.extract(blob) if blob else None,
                       lambda: fname):
            found = invoices.find_cups(source() or "")
            if found:
                d["cups"] = found
                break
    hard_signals = ("cups", "power_p1_kw", "energy_p1_eur_kwh", "energy_p2_eur_kwh",
                    "energy_p3_eur_kwh", "margin_eur_kwh", "power_p1_eur_kw_day",
                    "meter_rental_eur_month", "total_eur", "billing_period_start")
    if not any(d.get(k) is not None for k in hard_signals) and not d.get("components"):
        raise HTTPException(422, detail="El documento no parece una factura ni un "
                            "contrato de electricidad. Sube tu factura de la luz.")
    # Full markdown included so the caller can ARCHIVE it with the invoice —
    # the plain-language explanation then works from the real line items.
    return {"extracted": d, "markdown": md[:60000],
            "markdown_preview": md[:1500], "vlm_used": use_vlm}


# ── Tariff catalog (mig 004): retailer×product reference tariffs ──────────────
# Reads = any authenticated member (the selector / prefill source). Writes =
# admin (populate from a reviewed extraction or by hand). Holds the REGULATED
# 2.0TD peajes/cargos that must not be guessed from a signed PDF.
class CatalogIn(BaseModel):
    retailer: str
    product_name: str
    contract_type: str = Field("fixed", pattern="^(pvpc|fixed|indexed)$")
    access_tariff: str = "2.0TD"
    energy_p1_eur_kwh: Optional[float] = Field(None, ge=0, lt=10)
    energy_p2_eur_kwh: Optional[float] = Field(None, ge=0, lt=10)
    energy_p3_eur_kwh: Optional[float] = Field(None, ge=0, lt=10)
    margin_eur_kwh: Optional[float] = Field(None, ge=0, lt=1)
    passthru_p1_eur_kwh: Optional[float] = Field(None, ge=0, lt=1)
    passthru_p2_eur_kwh: Optional[float] = Field(None, ge=0, lt=1)
    passthru_p3_eur_kwh: Optional[float] = Field(None, ge=0, lt=1)
    power_p1_eur_kw_day: Optional[float] = Field(None, ge=0, lt=5)
    power_p2_eur_kw_day: Optional[float] = Field(None, ge=0, lt=5)
    meter_rental_eur_month: Optional[float] = Field(None, ge=0, lt=20)
    electricity_tax_pct: Optional[float] = Field(None, ge=0, le=15)
    vat_pct: Optional[float] = Field(None, ge=0, le=21)
    components: Optional[Dict[str, Any]] = None
    source_url: Optional[str] = None
    valid_from: Optional[_date] = None
    confidence: Optional[float] = Field(None, ge=0, le=1)
    notes: Optional[str] = None
    active: bool = True


class CatalogUpdate(BaseModel):
    retailer: Optional[str] = None
    product_name: Optional[str] = None
    contract_type: Optional[str] = Field(None, pattern="^(pvpc|fixed|indexed)$")
    access_tariff: Optional[str] = None
    energy_p1_eur_kwh: Optional[float] = Field(None, ge=0, lt=10)
    energy_p2_eur_kwh: Optional[float] = Field(None, ge=0, lt=10)
    energy_p3_eur_kwh: Optional[float] = Field(None, ge=0, lt=10)
    margin_eur_kwh: Optional[float] = Field(None, ge=0, lt=1)
    passthru_p1_eur_kwh: Optional[float] = Field(None, ge=0, lt=1)
    passthru_p2_eur_kwh: Optional[float] = Field(None, ge=0, lt=1)
    passthru_p3_eur_kwh: Optional[float] = Field(None, ge=0, lt=1)
    power_p1_eur_kw_day: Optional[float] = Field(None, ge=0, lt=5)
    power_p2_eur_kw_day: Optional[float] = Field(None, ge=0, lt=5)
    meter_rental_eur_month: Optional[float] = Field(None, ge=0, lt=20)
    electricity_tax_pct: Optional[float] = Field(None, ge=0, le=15)
    vat_pct: Optional[float] = Field(None, ge=0, le=21)
    components: Optional[Dict[str, Any]] = None
    source_url: Optional[str] = None
    valid_from: Optional[_date] = None
    confidence: Optional[float] = Field(None, ge=0, le=1)
    notes: Optional[str] = None
    active: Optional[bool] = None


@router.get("/components-base")
async def components_base(access_tariff: str = Query("2.0TD"),
                          ctx: ConsumContext = Depends(consum_context)):
    """The regulated component base from api_exo (SSOT) — seeds the indexed
    matrix in the contract form and carries the IVA/IEE scalars + verified
    flags. Falls back to the vendored defaults when api_exo is unreachable."""
    from app.services import tariff_components as tc
    payload = await exo_client.tariff_components(access_tariff)
    if payload:
        return {"source": "api_exo", **payload}
    return {
        "source": "defaults", "access_tariff": access_tariff,
        "components": {cid: {"bands": bands, "unit": tc.UNIT[cid],
                             "source": tc.SOURCE[cid], "verified": False}
                       for cid, bands in tc.default_components(access_tariff).items()},
        "all_verified": False,
    }


# NOTE: /catalog/retailers MUST be declared before /catalog/{cid} so "retailers"
# is not parsed as an int id.
@router.get("/catalog/retailers")
async def catalog_retailers(ctx: ConsumContext = Depends(consum_context)):
    return {"retailers": await tariff_catalog.list_retailers()}


@router.get("/catalog")
async def catalog_list(retailer: Optional[str] = Query(None),
                       ctx: ConsumContext = Depends(consum_context)):
    return {"values": await tariff_catalog.list_products(retailer)}


@router.get("/catalog/resolve")
async def catalog_resolve(retailer: Optional[str] = Query(None),
                          product: Optional[str] = Query(None),
                          ctx: ConsumContext = Depends(consum_context)):
    """Match an extracted retailer/product against the catalog (upload step 2:
    a confident match settles the contract type without asking the user)."""
    hit = await tariff_catalog.resolve(retailer, product)
    return hit or {"match": None}


@router.get("/catalog/{cid}")
async def catalog_get(cid: int, ctx: ConsumContext = Depends(consum_context)):
    row = await tariff_catalog.get(cid)
    if not row:
        raise HTTPException(404, detail="Producto no encontrado")
    return row


# The catalog is a GLOBAL shared reference (all orgs read it) — writes are
# platform-staff only. require_role("admin") is NOT enough: a household
# owner/admin satisfies it and could edit every org's catalog.
def _require_staff(ctx: ConsumContext) -> None:
    if not ctx.is_superadmin:
        raise HTTPException(403, detail="Solo el personal de la plataforma puede editar el catálogo")


@router.post("/catalog", status_code=201)
async def catalog_create(body: CatalogIn,
                         ctx: ConsumContext = Depends(consum_context)):
    _require_staff(ctx)
    return await tariff_catalog.create(body.model_dump(), (ctx.user or {}).get("email"))


@router.put("/catalog/{cid}")
async def catalog_update(cid: int, body: CatalogUpdate,
                         ctx: ConsumContext = Depends(consum_context)):
    _require_staff(ctx)
    return await tariff_catalog.update(cid, body.model_dump(exclude_unset=True),
                                       (ctx.user or {}).get("email"))


@router.delete("/catalog/{cid}")
async def catalog_delete(cid: int,
                         ctx: ConsumContext = Depends(consum_context)):
    _require_staff(ctx)
    await tariff_catalog.delete(cid)
    return {"deleted": cid}
