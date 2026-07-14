"""Playground de extracción de facturas — SOLO superadmin.

Sirve para probar/afinar la lectura de facturas de CUALQUIER comercializadora
(no solo las de un hogar dado de alta) antes de confiar en la extracción real
del flujo de usuario (`POST /contracts/extract`). Reutiliza el MISMO pipeline
(convert_client → ContractSkill.extract_tariff_sheet + InvoiceSkill.
extract_bill_amounts) pero:

  · NO exige el guard 422 "no parece un documento eléctrico" — aquí interesa
    ver también los fallos, así que ese caso vuelve como `warning` en el 200.
  · GARANTÍA DE NO-PERSISTENCIA: esta ruta NUNCA escribe en la base de datos.
    No crea ni toca `consum.contracts`, `consum.invoices` ni ningún CUPS —
    todo el resultado vive en la respuesta HTTP, nada se guarda server-side.
    Si en el futuro se añade cualquier INSERT/UPDATE/DELETE a este módulo,
    revisa primero este comentario: rompería la promesa del playground.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.api.v1.dependencies.rbac import ConsumContext, consum_context
from app.core.config import settings
from app.services import ai_client, convert_client
from app.services.ai import registry

router = APIRouter(prefix="/playground", tags=["playground"])

_MAX_UPLOAD = 8 * 1024 * 1024  # 8 MB — same cap as /contracts/extract

# Mismas señales "duras" que el guard de /contracts/extract, pero aquí SOLO se
# usan para decidir si añadir un warning — nunca para devolver un error.
_HARD_SIGNALS = ("cups", "power_p1_kw", "energy_p1_eur_kwh", "energy_p2_eur_kwh",
                 "energy_p3_eur_kwh", "margin_eur_kwh", "power_p1_eur_kw_day",
                 "meter_rental_eur_month", "total_eur", "billing_period_start")


def _require_superadmin(ctx: ConsumContext) -> None:
    if not ctx.is_superadmin:
        raise HTTPException(403, detail="Playground de extracción: solo superadmin.")


@router.post("/extract")
async def playground_extract(
        file: Optional[UploadFile] = File(None),
        text: Optional[str] = Form(None),
        use_vlm: bool = Form(False),
        ctx: ConsumContext = Depends(consum_context)):
    """Sube una factura/ficha/contrato de CUALQUIER comercializadora y devuelve
    lo que el modelo extrae — tarifa (ExtractedTariff) + importes (BillAmounts)
    — junto con el markdown convertido y metadatos de inferencia.

    Superadmin only. No guarda nada: ni invoice, ni contrato, ni CUPS — es una
    herramienta de comparación/afinado entre proveedores, no un flujo de alta.
    """
    _require_superadmin(ctx)
    if not ai_client.configured():
        raise HTTPException(503, detail="La lectura con IA no está disponible ahora.")

    md: Optional[str] = None
    if file is not None:
        blob = await file.read()
        if len(blob) > _MAX_UPLOAD:
            raise HTTPException(413, detail="El archivo es demasiado grande (máx. 8 MB).")
        fname = file.filename or "documento"
        ctype = file.content_type or "application/octet-stream"
        if ctype.startswith("text/") or fname.lower().endswith((".md", ".txt", ".markdown")):
            md = blob.decode("utf-8", errors="ignore").strip() or None
        else:
            md = await convert_client.to_markdown(blob, fname, ctype, use_vlm=use_vlm)
        if not md:
            raise HTTPException(502, detail="No se pudo leer el documento. Prueba con otra "
                                             "foto o pega el texto.")
    elif text and text.strip():
        md = text.strip()
    else:
        raise HTTPException(400, detail="Sube un archivo o pega el texto a probar.")

    started_at = datetime.now(timezone.utc).isoformat()
    t0 = time.monotonic()

    contract_skill = registry.get("contract")
    invoice_skill = registry.get("invoice")
    tariff = await contract_skill.extract_tariff_sheet(md)
    amounts = await invoice_skill.extract_bill_amounts(md)

    elapsed_ms = round((time.monotonic() - t0) * 1000)

    if tariff is None and amounts is None:
        raise HTTPException(503, detail="La IA no pudo interpretar el documento.")

    extracted = tariff.model_dump() if tariff is not None else None
    warning = None
    if tariff is None:
        # El playground es una herramienta de diagnóstico: si al menos los
        # importes salieron, se muestran junto con el markdown en vez de
        # perder toda la lectura con un 503.
        warning = ("La parte de tarifa no se pudo interpretar (JSON inválido "
                   "tras reintentos) — revisa el markdown crudo. Los importes "
                   "sí se extrajeron.")
    elif not any(extracted.get(k) is not None for k in _HARD_SIGNALS) and not extracted.get("components"):
        # A diferencia de /contracts/extract, NUNCA bloqueamos con 422 aquí: el
        # playground existe precisamente para ver estos casos límite.
        warning = ("El documento no muestra señales eléctricas claras (CUPS, "
                   "precios, potencia...). Puede que la comercializadora use un "
                   "formato distinto — revisa el markdown crudo.")

    return {
        "extracted": extracted,
        "amounts": amounts.model_dump() if amounts is not None else None,
        "markdown": md[:60000],
        "warning": warning,
        "meta": {
            "model": settings.CHAT_MODEL or None,
            "elapsed_ms": elapsed_ms,
            "started_at": started_at,
        },
    }
