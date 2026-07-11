"""AI endpoints (F4) — daily narrative + Q&A over the household's AGGREGATES.

Privacy rule (ADR): the LLM only ever receives aggregates — KPIs, daily/hour
totals, OE3 insights, exogenous context. Never raw sensor rows, device MACs,
emails or names. Gates: require_ai (ServiceAccess.ai_enabled). Cost control:
narrative cached per scope·day (24 h); /ask rate-limited per org·day.
"""
from __future__ import annotations

import time
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from app.api.v1.dependencies.rbac import ConsumContext, require_ai
from app.api.v1.endpoints.consumption import _slugs
from app.core.config import settings
from app.services import ai_client, consumption, exo_client, oe3

router = APIRouter(prefix="/ai", tags=["ai"])

_narrative_cache: dict = {}    # scope -> (day, text)
_ask_counter: dict = {}        # scope -> (day, count)

_SYSTEM = (
    "Eres el asesor energético de CONSUM-IA (pibiCo) para un hogar en España "
    "con tarifa PVPC. Recibes SOLO datos agregados del hogar (kWh, costes, "
    "insights) y contexto exógeno (precios, clima, solar, carbono). Responde "
    "en el idioma del usuario, claro y accionable, sin tecnicismos "
    "innecesarios, máximo 3 párrafos cortos o viñetas. Nunca inventes cifras: "
    "usa únicamente las del contexto. Si falta un dato, dilo. Escribe las "
    "cifras en texto plano (nunca notación LaTeX ni $...$)."
)


async def _aggregate_context(slugs: list[str]) -> dict:
    """The ONLY payload the LLM sees — aggregates, no raw rows / PII."""
    summ = await consumption.summary(slugs)
    power = await consumption.current_power(slugs)
    shift = await oe3.shift_analysis(slugs)
    thermal = await oe3.thermal_analysis(slugs)
    window = await oe3.green_window()
    pvpc = await exo_client.pvpc_day("today")
    carbon = await exo_client.carbon_current()
    thermal.pop("series", None)          # keep it aggregate-sized
    window.pop("hours", None)
    return {
        "fecha": date.today().isoformat(),
        "kwh": {"hoy": summ.get("today_kwh"), "7d": summ.get("week_kwh"),
                "30d": summ.get("month_kwh")},
        "potencia_actual_w": power.get("total_w"),
        "n_dispositivos": len(power.get("devices") or []),
        "oe3_desplazamiento": shift,
        "oe3_clima": thermal,
        "oe3_ventana": window,
        "pvpc_hoy": [
            {"hour": p.get("hour"),
             "eur_kwh": p.get("price_eur_kwh") or ((p.get("price_eur_mwh") or 0) / 1000.0),
             "period": p.get("period")}
            for p in (pvpc or {}).get("prices") or []
        ],
        "carbono": {"gco2_kwh": (carbon or {}).get("intensity_gco2_kwh"),
                    "banda": (carbon or {}).get("band")},
    }


@router.get("/status")
async def status(ctx: ConsumContext = Depends(require_ai())):
    """Whether AI is usable for this caller (page decides narrative vs upsell)."""
    return {"configured": ai_client.configured(),
            "provider": settings.CHAT_PROVIDER if ai_client.configured() else None,
            "ask_daily_limit": settings.AI_ASK_DAILY_LIMIT}


@router.get("/narrative")
async def narrative(customer: Optional[str] = Query(None),
                    refresh: bool = Query(False),
                    ctx: ConsumContext = Depends(require_ai())):
    """Today's narrative for the household — cached per scope for the day."""
    if not ai_client.configured():
        raise HTTPException(503, detail={"code": "AI_NOT_CONFIGURED",
                                         "message": "La IA aún no está configurada en el servidor."})
    slugs = await _slugs(ctx, customer)
    if not slugs:
        raise HTTPException(404, detail="no household in scope")
    scope = "|".join(sorted(slugs))
    today = date.today().isoformat()
    hit = _narrative_cache.get(scope)
    if hit and hit[0] == today and not refresh:
        return {"date": today, "narrative": hit[1], "cached": True}

    import json
    context = await _aggregate_context(slugs)
    text = await ai_client.llm_chat([
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content":
            "Con este contexto agregado del hogar, escribe la narrativa del día: "
            "qué tal va el consumo, qué explica el clima, cuánto se puede ahorrar "
            "y cuál es la mejor franja para los consumos aplazables.\n\n"
            + json.dumps(context, ensure_ascii=False)},
    ], temperature=0.4, max_tokens=700)
    if not text:
        raise HTTPException(502, detail={"code": "AI_UPSTREAM",
                                         "message": "El servicio de IA no respondió."})
    _narrative_cache[scope] = (today, text)
    return {"date": today, "narrative": text, "cached": False}


@router.post("/ask")
async def ask(question: str = Body(..., embed=True, max_length=500),
              history: List[dict] = Body(default=[], embed=True),
              customer: Optional[str] = Body(None, embed=True),
              ctx: ConsumContext = Depends(require_ai())):
    """One Q&A turn about the household's consumption. `history` is the
    client-side transcript (last few turns), re-sent each call — stateless."""
    if not ai_client.configured():
        raise HTTPException(503, detail={"code": "AI_NOT_CONFIGURED",
                                         "message": "La IA aún no está configurada en el servidor."})
    slugs = await _slugs(ctx, customer)
    if not slugs:
        raise HTTPException(404, detail="no household in scope")
    scope = "|".join(sorted(slugs))
    today = date.today().isoformat()

    day, count = _ask_counter.get(scope, (today, 0))
    if day != today:
        day, count = today, 0
    if count >= settings.AI_ASK_DAILY_LIMIT:
        raise HTTPException(429, detail={"code": "AI_RATE_LIMIT",
                                         "message": f"Límite diario de {settings.AI_ASK_DAILY_LIMIT} preguntas alcanzado."})
    _ask_counter[scope] = (day, count + 1)

    import json
    context = await _aggregate_context(slugs)
    messages = [{"role": "system", "content": _SYSTEM +
                 "\n\nContexto agregado del hogar (hoy):\n" +
                 json.dumps(context, ensure_ascii=False)}]
    for turn in history[-6:]:                      # last 3 exchanges max
        role = turn.get("role")
        content = str(turn.get("content") or "")[:1000]
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question})

    text = await ai_client.llm_chat(messages, temperature=0.5, max_tokens=700)
    if not text:
        raise HTTPException(502, detail={"code": "AI_UPSTREAM",
                                         "message": "El servicio de IA no respondió."})
    return {"answer": text, "remaining_today": settings.AI_ASK_DAILY_LIMIT - count - 1}
