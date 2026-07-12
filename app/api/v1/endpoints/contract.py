"""Supply-contract endpoints (entidad contrato) — api_consum business domain.

Reads follow the usual org-scoped `_slugs` pattern; writes need role editor+
of the owning org (superadmin any household; plain service key read-only).
The active contract drives ALL €-costing via services/pricing.py — households
without one keep costing at PVPC exactly as before.
"""
from __future__ import annotations

import re
from datetime import date as _date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator

from app.api.v1.dependencies.rbac import (ConsumContext, consum_context,
                                          require_role, require_tier)
from app.api.v1.endpoints.consumption import _slugs
from app.services import consumption, contracts, pricing

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
        if self.contract_type == "indexed" and self.margin_eur_kwh is None:
            raise ValueError("Contrato indexado: falta margin_eur_kwh")
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
        if self.contract_type == "indexed" and self.margin_eur_kwh is None:
            raise ValueError("Contrato indexado: falta margin_eur_kwh")
        return self


@router.get("")
async def list_contracts(customer: Optional[str] = Query(None),
                         ctx: ConsumContext = Depends(consum_context)):
    """All contracts (history included) in the caller's scope."""
    slugs = await _slugs(ctx, customer)
    return {"values": await contracts.list_for(slugs)}


@router.get("/active")
async def active(customer: Optional[str] = Query(None),
                 date: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
                 ctx: ConsumContext = Depends(consum_context)):
    """The contract covering `date` (default today) + the current-hour price.
    Feeds the Panel. Multi-household scopes → contract: null, fallback pvpc."""
    slugs = await _slugs(ctx, customer)
    on = date or _date.today().isoformat()
    cid, _ = await contracts.resolve_for_slugs(slugs, on, on)
    contract = await contracts.get_active(cid, on) if cid else None
    return {
        "contract": contract,
        "fallback": None if contract else "pvpc",
        "price_now": await pricing.price_now(contract),
    }


@router.post("", status_code=201)
async def create(body: ContractIn,
                 ctx: ConsumContext = Depends(require_role("editor"))):
    ctx.check_slug(body.customer)
    payload = body.model_dump(exclude={"customer"})
    return await contracts.create(body.customer, payload,
                                  (ctx.user or {}).get("email"))


@router.put("/{contract_id}")
async def update(contract_id: int, body: ContractUpdate,
                 ctx: ConsumContext = Depends(require_role("editor"))):
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
                 ctx: ConsumContext = Depends(require_role("admin"))):
    """Hard delete (admin+). The UI favours end-dating instead — history feeds
    future invoicing of past periods."""
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
    slugs = await _slugs(ctx, customer)
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
