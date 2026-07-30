"""Ausencias / vacaciones (2026-07-30) — member-facing CRUD for declared
away windows, per CUPS (`?supply=`-aware, same resolution pattern as every
other `?supply=` endpoint — see `endpoints/consumption.py`'s `_sp` helper).

Not a service-key write path (unlike solar-config/comfort-flex there's no
cross-team PLC-sync contract here) — any authenticated household member may
declare/remove their own household's absences; a bare read-only service key
is refused on writes, mirroring `put_solar_config`/`put_comfort_flex`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from app.api.v1.dependencies.rbac import ConsumContext, consum_context
from app.api.v1.endpoints.consumption import _slugs, _sp
from app.services import absences as absences_svc
from app.services import consumption

router = APIRouter(prefix="/absences", tags=["absences"])


@router.get("")
async def list_absences(customer: Optional[str] = Query(None),
                        supply: int = Query(..., description="supply_point_id (CUPS)"),
                        ctx: ConsumContext = Depends(consum_context)):
    """Declared absences for ONE CUPS — `supply` is required (an absence is
    always per installation, never household-wide)."""
    slugs = await _slugs(ctx, customer)
    if not slugs:
        raise HTTPException(404, detail="no household in scope")
    sp = await _sp(slugs, supply)
    if not sp:
        raise HTTPException(404, detail="Punto de suministro desconocido")
    return {"data": await absences_svc.list_for(sp["id"])}


class AbsenceIn(BaseModel):
    customer: Optional[str] = None
    supply: int
    starts_at: datetime
    ends_at: datetime
    label: Optional[str] = Field(None, max_length=200)

    @field_validator("starts_at", "ends_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("starts_at/ends_at deben incluir zona horaria (tz-aware)")
        return v


@router.post("")
async def create_absence(body: AbsenceIn, ctx: ConsumContext = Depends(consum_context)):
    if ctx.is_service and not ctx.is_superadmin:
        raise HTTPException(403, detail="read-only service key")
    slugs = await _slugs(ctx, body.customer)
    if not slugs:
        raise HTTPException(400, detail="no household in scope")
    sp = await _sp(slugs, body.supply)
    if not sp:
        raise HTTPException(404, detail="Punto de suministro desconocido")
    if body.ends_at <= body.starts_at:
        raise HTTPException(400, detail="ends_at debe ser posterior a starts_at")
    if (body.ends_at - body.starts_at).days > absences_svc.MAX_ABSENCE_DAYS:
        raise HTTPException(400, detail=f"rango máximo {absences_svc.MAX_ABSENCE_DAYS} días")
    return await absences_svc.create(
        sp["id"], body.starts_at, body.ends_at, body.label,
        created_by=(ctx.user or {}).get("email"))


@router.delete("/{absence_id}")
async def delete_absence(absence_id: int, customer: Optional[str] = Query(None),
                         ctx: ConsumContext = Depends(consum_context)):
    """Only the member's OWN CUPS — `absences.delete()` re-checks ownership
    via a join against `consum.supply_points`, scoped to the caller's
    already-authorized customer_ids (never trusts the bare id alone)."""
    if ctx.is_service and not ctx.is_superadmin:
        raise HTTPException(403, detail="read-only service key")
    slugs = await _slugs(ctx, customer)
    if not slugs:
        raise HTTPException(404, detail="no household in scope")
    ids = await consumption._slugs_to_ids(slugs)
    ok = await absences_svc.delete(absence_id, list(ids.values()))
    if not ok:
        raise HTTPException(404, detail="ausencia no encontrada")
    return {"deleted": True}
