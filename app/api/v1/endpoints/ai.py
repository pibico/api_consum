"""AI endpoints (F4) — daily narrative + AGENTIC Q&A over the household data.

Privacy rule (ADR, revised 2026-07-13 by owner mandate): /narrative keeps the
aggregates-only contract; /ask may additionally reach PER-HOUSEHOLD detail
through datatools (sensor series with their PLC names, the household's OWN
invoices and their text) — never another tenant's anything, emails redacted.
Gates: require_ai (ServiceAccess.ai_enabled). Cost control: narrative cached
per scope·day (24 h); /ask rate-limited per org·day, ≤5 tool hops per turn.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, datetime
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from app.api.v1.dependencies.rbac import ConsumContext, require_ai
from app.api.v1.endpoints.consumption import _slugs
from app.core.config import settings
from app.services import ai_client, consumption, exo_client, oe3

router = APIRouter(prefix="/ai", tags=["ai"])
logger = logging.getLogger("consum.ai.audit")

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
    summ, power, shift, thermal, window, pvpc, carbon = await asyncio.gather(
        consumption.summary(slugs),
        consumption.current_power(slugs),
        oe3.shift_analysis(slugs),
        oe3.thermal_analysis(slugs),
        oe3.green_window(),
        exo_client.pvpc_day("today"),
        exo_client.carbon_current(),
    )
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


def _audit_context(scope: str, kind: str, context: dict) -> None:
    """AUDIT (F6): log exactly what leaves for the LLM, and hard-fail if
    anything PII-shaped slipped in. The context is aggregates by
    construction; this is the tripwire if someone extends it carelessly."""
    import json as _json
    blob = _json.dumps(context, ensure_ascii=False)
    if "@" in blob:  # emails are the realistic leak vector here
        logger.error("AI context REJECTED (PII suspect) scope=%s kind=%s", scope, kind)
        raise HTTPException(500, detail="AI context failed the PII tripwire")
    logger.info("AI-AUDIT scope=%s kind=%s bytes=%d context=%s",
                scope, kind, len(blob), blob)


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
    _audit_context(scope, "narrative", context)
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
async def ask(question: str = Body(..., embed=True, max_length=6000),
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
    # CREDIT CUTOFF: monthly token budget per scope (the ai_usage ledger is
    # also the pay-per-use billing source).
    from app.services import ai_usage
    used = await ai_usage.month_tokens(scope)
    if used >= settings.AI_MONTHLY_TOKEN_CAP:
        raise HTTPException(429, detail={
            "code": "AI_CREDITS",
            "message": "Has agotado los créditos de IA de este mes."})
    _ask_counter[scope] = (day, count + 1)

    import json

    from app.services.ai import datatools

    context = await _aggregate_context(slugs)
    _audit_context(scope, "ask", context)
    messages = [{"role": "system", "content": _SYSTEM +
                 "\n\nContexto agregado del hogar (hoy):\n" +
                 json.dumps(context, ensure_ascii=False) +
                 "\n\nTienes HERRAMIENTAS para consultar los datos reales del "
                 "hogar (sensores, históricos, facturas): úsalas siempre que "
                 "la pregunta pida cifras que no estén en el contexto. Nunca "
                 "menciones las herramientas al usuario. REGLAS: las facturas "
                 "cubren SU periodo, no meses naturales — si preguntan por un "
                 "mes, aclara qué periodos lo cubren y no sumes facturas como "
                 "si fueran el mes. Para importes de conceptos (peajes, "
                 "impuestos, bono social, alquiler) usa la herramienta "
                 "`invoice` o `invoice_search` — NUNCA los deduzcas. Para "
                 "potencia contratada/ICP usa `peaks` con threshold_w: si los "
                 "picos SUPERAN la potencia contratada, dilo claramente y "
                 "explica que el ICP corta con excesos sostenidos (picos "
                 "breves pasan) — NUNCA digas que no hay riesgo si los datos "
                 "muestran picos por encima. El dato `carbono` del contexto "
                 "es la intensidad de la RED (mix nacional): antes de "
                 "atribuir CO2 al CONSUMO del hogar, consulta `tariff` — si "
                 "origen_renovable es true, las emisiones de su consumo son "
                 "0 (garantía de origen) y la intensidad de red es solo "
                 "informativa. Responde en texto llano, sin tablas Markdown. "
                 "Hoy es " + date.today().isoformat() + "."}]
    for turn in history[-6:]:                      # last 3 exchanges max
        role = turn.get("role")
        content = str(turn.get("content") or "")[:1000]
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": question})

    # Agentic loop (2026-07-13, supersedes the aggregates-only ADR by owner
    # mandate): the model may call up to 4 HOUSEHOLD-SCOPED tools before
    # answering. gpt-oss emits NATIVE tool_calls (ollama channel) even from a
    # prompt-level spec, with empty content — so we consume both channels:
    # structured tool_calls first, {"tool": ...} in the text as fallback (any
    # provider). Every tool result is size-audited; invoice text is
    # email-redacted inside datatools.
    def _parse_text_call(reply: str):
        s = (reply or "").strip()
        if s.startswith("```"):
            s = s.strip("`").strip()
            if s[:4].lower() == "json":
                s = s[4:].strip()
        if s.startswith("{") and '"tool"' in s[:80]:
            try:
                parsed = json.loads(s)
                if isinstance(parsed, dict) and parsed.get("tool"):
                    return {"name": str(parsed["tool"]),
                            "args": parsed.get("args") or {}}
            except ValueError:
                pass
        return None

    text = None
    tok_prompt = tok_completion = hops = 0
    started_at = datetime.now().strftime("%H:%M:%S")
    t0 = time.monotonic()
    for _hop in range(6):
        # NATIVE tool protocol (verified with the gateway/gpt-oss): schemas go
        # in `tools`; each executed call is replayed as an assistant message
        # WITH tool_calls plus a role='tool' result — then the model answers.
        use_tools = datatools.NATIVE_TOOLS if _hop < 5 else None
        if _hop == 5:
            messages.append({"role": "user", "content":
                             "Responde YA al usuario en texto normal con la "
                             "información obtenida."})
        got = await ai_client.llm_chat_full(
            messages, temperature=0.2, max_tokens=900, tools=use_tools)
        if not got:
            break
        u = got.get("usage") or {}
        tok_prompt += u.get("prompt_tokens") or 0
        tok_completion += u.get("completion_tokens") or 0
        hops += 1
        calls = got.get("tool_calls") or []
        reply = got.get("text") or ""
        if not calls:
            fallback = _parse_text_call(reply)
            if fallback:
                fallback["id"] = "call_0"
                calls = [fallback]
                got["raw_tool_calls"] = [{"id": "call_0", "type": "function",
                                          "function": {
                    "name": fallback["name"], "arguments": fallback["args"]}}]
            else:
                text = reply or None
                break
        # Execute EVERY call of the turn (gpt-oss usually emits one).
        messages.append({"role": "assistant", "content": reply,
                         "tool_calls": got.get("raw_tool_calls") or []})
        for i, call in enumerate(calls):
            name = call["name"]
            if name.startswith("tool_"):
                name = name[5:]
            result = await datatools.run_tool(slugs, name, call.get("args") or {})
            blob = json.dumps(result, ensure_ascii=False)[:6000]
            logger.info("AI-AUDIT scope=%s kind=tool:%s bytes=%d",
                        scope, name, len(blob))
            # tool_call_id is REQUIRED for anthropic-style providers (the
            # gateway maps it to tool_result.tool_use_id); ollama ignores it.
            messages.append({"role": "tool", "content": blob, "name": name,
                             "tool_call_id": call.get("id") or f"call_{i}"})
    email = (ctx.user or {}).get("email")
    await ai_usage.record(scope, email, "ask", tok_prompt, tok_completion,
                          tool_hops=max(hops - 1, 0))
    if not text:
        raise HTTPException(502, detail={"code": "AI_UPSTREAM",
                                         "message": "El servicio de IA no respondió."})
    # Per-turn inference metadata — rendered under the bubble and persisted
    # with the assistant turn so it survives page reloads.
    meta = {"model": settings.CHAT_MODEL, "started_at": started_at,
            "elapsed_ms": int((time.monotonic() - t0) * 1000),
            "prompt_tokens": tok_prompt, "completion_tokens": tok_completion,
            "tool_hops": max(hops - 1, 0)}
    # Persist the day's conversation — the /ai page restores it on load.
    await ai_usage.chat_append(scope, email, "user", question)
    await ai_usage.chat_append(scope, email, "assistant", text, meta=meta)
    return {"answer": text,
            "remaining_today": settings.AI_ASK_DAILY_LIMIT - count - 1,
            "meta": meta,
            "tokens": {"prompt": tok_prompt, "completion": tok_completion},
            "credits_used_month": used + tok_prompt + tok_completion,
            "credits_cap": settings.AI_MONTHLY_TOKEN_CAP}


@router.get("/chat")
async def chat_today(customer: Optional[str] = Query(None),
                     ctx: ConsumContext = Depends(require_ai())):
    """Today's persisted conversation for this household+user — restores the
    /ai page after navigation."""
    from app.services import ai_usage
    slugs = await _slugs(ctx, customer)
    if not slugs:
        raise HTTPException(404, detail="no household in scope")
    scope = "|".join(sorted(slugs))
    turns = await ai_usage.chat_get(scope, (ctx.user or {}).get("email"))
    return {"turns": turns}
