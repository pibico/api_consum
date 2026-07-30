"""AdviceSkill (S6) — narrates a DETERMINISTIC forecast attribution.

The numbers (forecast kWh/€, band, SHAP contributions) are already computed
by `oe3.forecast_explained` + `shap_attr` — this skill's ONLY job is turning
them into 2-4 warm, jargon-free sentences. It NEVER invents a driver or
changes the verdict (same discipline as `InvoiceSkill.narrate_anomaly`).

Bypasses `Skill.run_json` (which discards token usage) and calls
`ai_client.llm_chat_full` directly so `ai_usage.record()` gets real
prompt/completion counts — mirrors the accounting pattern in
`endpoints/ai.py`'s `/ask` loop.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, ValidationError

from app.services import ai_client
from app.services.ai.base import Skill, _strip_json
from app.services.ai.equipment_guard import (fallback_tip as _equipment_fallback_tip,
                                             hits as _equipment_hits,
                                             missing_terms as _equipment_missing_terms)

logger = logging.getLogger("consum.ai.advice")

_SYSTEM = (
    "Eres el asistente de CONSUM-IA. Te doy la previsión de consumo/coste de "
    "un hogar YA CALCULADA (kWh, €, y sus causas — 'drivers' — con su efecto "
    "en kWh, ya ordenados de mayor a menor impacto) y, si aplica, un aviso "
    "meteorológico. Tu trabajo es SOLO redactar, en español o inglés según se "
    "te pida, con calidez y SIN jerga (nunca P1/P2/P3, HDD/CDD, Shapley, "
    "'atribución'; di 'horas caras/baratas', 'frío/calor'). Para el driver "
    "'calendario' usa EXACTAMENTE la etiqueta (y el detalle, si te lo doy) "
    "que te paso — p.ej. 'día laborable' o 'fin de semana / festivo' — y "
    "NUNCA asumas fin de semana/festivo si la etiqueta dice día laborable "
    "(ni al revés): la cifra puede ser negativa en un laborable (MENOS "
    "consumo que la media), nunca lo narres como si fuera festivo.\n"
    "- summary: 1-2 frases con la cifra clave (kWh y/o €) y la causa "
    "principal (el primer driver de la lista).\n"
    "- advice: 1 a 3 consejos concretos e imperativos (mover un "
    "electrodoméstico a una hora barata, prepararse para el aviso "
    "meteorológico si lo hay, etc.), coherentes con los drivers.\n"
    "NO inventes cifras que no te dé. NO cambies el veredicto (si dice que "
    "sube, no digas que baja). Responde SOLO el objeto JSON pedido."
)

_SYSTEM_EN = (
    "You are the CONSUM-IA assistant. I give you an ALREADY-COMPUTED "
    "household consumption/cost forecast (kWh, €, and its causes — "
    "'drivers', with their kWh effect, sorted by impact) and, if applicable, "
    "a weather advisory. Your ONLY job is to phrase it warmly and jargon-free "
    "(never P1/P2/P3, HDD/CDD, Shapley, 'attribution'; say 'expensive/cheap "
    "hours', 'cold/heat'). For the 'calendar' driver, use EXACTLY the label "
    "(and detail, if given) I pass you — e.g. 'weekday' or 'weekend/holiday' "
    "— and NEVER assume weekend/holiday if the label says weekday (or vice "
    "versa): the figure can be negative on a weekday (LESS consumption than "
    "average) — never narrate that as if it were a holiday.\n"
    "- summary: 1-2 sentences with the key figure (kWh and/or €) and the main "
    "cause (the first driver in the list).\n"
    "- advice: 1 to 3 concrete, imperative tips (shift an appliance to a "
    "cheap hour, prepare for the weather advisory if present, etc.), "
    "consistent with the drivers.\n"
    "Do NOT invent figures I did not give you. Do NOT change the verdict. "
    "Reply with ONLY the requested JSON object."
)


# ── Anomaly narration (Phase 2 E4 -> S-anomaly loop closure, 2026-07-30) ────
# Distinct system prompt from the forecast one above: an anomaly is a
# PAST/CURRENT statistical deviation already detected (z-score vs the same
# hour-of-week baseline), not a forward-looking prediction — narrating it
# with the forecast prompt's future tense ("vas a consumir...") would be
# wrong. Same discipline otherwise: numbers-in/prose-out, no invented
# figures, same equipment guardrail (_scrub_equipment, shared below).

_SYSTEM_ANOMALY = (
    "Eres el asistente de CONSUM-IA. Te doy un EVENTO DE CONSUMO ANOMALO "
    "YA DETECTADO estadisticamente en un hogar (NO una prevision): la "
    "potencia media observada en una hora concreta frente a lo esperado "
    "para esa misma hora en semanas anteriores (mismo dia de la semana y "
    "hora), con su desviacion. Tu trabajo es SOLO redactar, en espanol o "
    "ingles segun se te pida, con calidez y SIN alarmismo ni jerga (nunca "
    "'z-score', 'desviacion estandar', 'Shapley', 'atribucion'; di 'mas de "
    "lo habitual'/'menos de lo habitual').\n"
    "- summary: 1-2 frases que digan QUE paso (mas o menos consumo de lo "
    "normal) y CUANDO (la franja horaria exacta que te doy) — nunca "
    "inventes una causa que no te haya dado.\n"
    "- advice: 1 a 2 consejos concretos y accionables (revisar si un "
    "electrodomestico se quedo encendido, comprobar el enchufe/dispositivo, "
    "vigilar si se repite en los proximos dias), coherentes con el sentido "
    "de la desviacion: si es una SUBIDA de consumo, sugiere revisar que no "
    "haya quedado algo encendido; si es una BAJADA, sugiere comprobar que "
    "un aparato no se haya quedado sin corriente/apagado sin querer — nunca "
    "mezcles ambos sentidos.\n"
    "NO inventes cifras que no te de. Responde SOLO el objeto JSON pedido."
)

_SYSTEM_ANOMALY_EN = (
    "You are the CONSUM-IA assistant. I give you a STATISTICAL CONSUMPTION "
    "ANOMALY already detected in a household (NOT a forecast): the average "
    "power observed in one specific hour versus what was expected for that "
    "same hour in previous weeks (same weekday and hour), with its "
    "deviation. Your ONLY job is to phrase it warmly and without alarmism "
    "or jargon (never 'z-score', 'standard deviation', 'Shapley', "
    "'attribution'; say 'higher/lower than usual').\n"
    "- summary: 1-2 sentences stating WHAT happened (more/less consumption "
    "than normal) and WHEN (the exact time window I give you) — never "
    "invent a cause I did not give you.\n"
    "- advice: 1 to 2 concrete, actionable tips (check whether an appliance "
    "was left on, check the plug/device, watch if it repeats in the coming "
    "days), consistent with the DIRECTION of the deviation: an INCREASE "
    "should suggest checking for something left running; a DECREASE should "
    "suggest checking an appliance wasn't accidentally left unpowered/off — "
    "never mix the two directions.\n"
    "Do NOT invent figures I did not give you. Reply with ONLY the "
    "requested JSON object."
)


def _anomaly_deterministic_summary(entry: Dict[str, Any], lang: str) -> str:
    """Numbers-only headline, no LLM — the safe fallback used both by
    `_anomaly_template_fallback` (AI off/capped) and `_scrub_equipment`'s
    rare repair path for the anomaly narration."""
    observed, expected = entry.get("observed_w"), entry.get("expected_w")
    label = entry.get("period_label") or ""
    if observed is not None and expected is not None:
        up = observed > expected
        if lang == "en":
            word = "higher" if up else "lower"
            return f"Unusual consumption between {label}: {word} than usual."
        word = "más alto" if up else "más bajo"
        return f"Consumo fuera de lo habitual entre {label}: {word} de lo habitual."
    return (f"Unusual consumption detected between {label}." if lang == "en"
           else f"Consumo fuera de lo habitual detectado entre {label}.")


def _facts_for_anomaly(entry: Dict[str, Any], lang: str,
                       equipment: Optional[Dict[str, Any]] = None) -> str:
    lines = [
        f"- franja_horaria: {entry.get('period_label')}",
    ]
    observed, expected = entry.get("observed_w"), entry.get("expected_w")
    if observed is not None:
        lines.append(f"- potencia_observada_w: {observed:.0f}")
    if expected is not None:
        lines.append(f"- potencia_esperada_w: {expected:.0f}")
    if entry.get("score") is not None:
        lines.append(f"- desviacion_z: {entry['score']:+.2f}")
    for c in (entry.get("attribution") or {}).get("contributions") or []:
        label, phi = c.get("label"), c.get("phi")
        if label and phi is not None:
            lines.append(f"- factor: {label} | efecto: {phi:+.2f}")
    equipment = equipment or {}
    missing = []
    if not equipment.get("electric_heating", False):
        missing.append("electric heating" if lang == "en" else "calefacción eléctrica")
    if not equipment.get("electric_cooling", False):
        missing.append("electric AC/HVAC" if lang == "en" else "aire acondicionado/HVAC eléctrico")
    if missing:
        if lang == "en":
            lines.append(f"- equipment_missing: This household has NO {' or '.join(missing)} "
                         "— NEVER suggest actions for equipment it doesn't have.")
        else:
            lines.append(f"- equipo_ausente: Este hogar NO tiene {' ni '.join(missing)} "
                         "— NUNCA sugieras acciones sobre equipos que no tiene.")
    return "\n".join(lines)


class AdviceNarrative(BaseModel):
    """LLM output — numbers-in, prose-out. Never the source of the numbers."""
    summary: str = Field(..., max_length=400)
    advice: List[str] = Field(default_factory=list, max_length=3)


# ── Equipment guardrail (hard requirement 2026-07-29) ───────────────────────
# A household without electric heating/cooling must NEVER be told to
# "precalentar"/"usar la bomba de calor"/"encender el A/C" — checked BOTH
# ways (ES+EN substrings, regardless of the reply's own language, since a
# jailbroken/mixed-language reply is still a risk) and enforced
# deterministically — the LLM's own discipline (system prompt) is defense
# layer 1, this filter is the one that actually blocks it.
#
# The term list + text-matching primitives live in `equipment_guard` (shared
# with the AEMET weather-alert banner's `energy_advisory` gating in
# `endpoints/consumption.py`, added 2026-07-29) so both surfaces enforce the
# SAME rule for the SAME comfort_flex profile.


def _deterministic_summary(entry: Dict[str, Any], lang: str) -> str:
    """Numbers-only headline, no driver mention — the safe fallback used
    both by `_template_fallback` (AI off/capped) and `_scrub_equipment`'s
    rare repair path (LLM slipped a forbidden term into the summary itself)."""
    kwh, eur = entry.get("kwh"), entry.get("eur")
    if lang == "en":
        return f"Forecast: {kwh} kWh" + (f" (~{eur} EUR)" if eur is not None else "") + "."
    return f"Previsión: {kwh} kWh" + (f" (~{eur} €)" if eur is not None else "") + "."


def _scrub_equipment(narrative: "AdviceNarrative", equipment: Optional[Dict[str, Any]],
                     entry: Dict[str, Any], lang: str,
                     fallback_summary: Optional[Any] = None) -> "AdviceNarrative":
    """Deterministic guardrail — drops any advice tip mentioning heating/
    cooling equipment the household doesn't have (case-insensitive, checked
    against BOTH language term lists). If that empties the tip list, adds
    ONE universal equipment-agnostic tip so the card/email never ships
    blank. Also repairs the rare case where the LLM slipped a forbidden
    term into `summary` itself (degrades to a deterministic figure-only
    line — the attribution driver is already excluded upstream, so this
    should essentially never trigger).

    `fallback_summary` (zero-arg callable, forecast-advice call sites omit
    it) lets `narrate_anomaly`/`_anomaly_template_fallback` reuse this SAME
    guardrail with an anomaly-shaped `entry` (observed/expected W, not
    kwh/eur) — shared logic, entry-shape-specific fallback text."""
    forbidden = _equipment_missing_terms(equipment)
    if not forbidden:
        return narrative

    def _hits(text: str) -> bool:
        return _equipment_hits(text, forbidden)

    kept = [a for a in narrative.advice if not _hits(a)]
    if not kept:
        kept = [_equipment_fallback_tip(lang)]
    fb = fallback_summary or (lambda: _deterministic_summary(entry, lang))
    summary = fb() if _hits(narrative.summary) else narrative.summary
    return AdviceNarrative(summary=summary, advice=kept)


def _facts_for_forecast(entry: Dict[str, Any], alert: Optional[Dict[str, Any]],
                        lang: str, equipment: Optional[Dict[str, Any]] = None) -> str:
    attribution = entry.get("attribution") or {}
    lines = [
        f"- fecha: {entry.get('date')}",
        f"- kwh_previsto: {entry.get('kwh')}",
    ]
    if entry.get("eur") is not None:
        lines.append(f"- coste_previsto_eur: {entry['eur']}")
    # NOTE: tariff period labels (P1/P2/P3) are deliberately NOT passed to the
    # LLM — they're jargon the system prompt explicitly forbids; the email's
    # "periods" field is rendered separately (deterministic), never narrated.
    for c in attribution.get("contributions") or []:
        # `label_en`/`detail_en` only exist on the `calendar` driver so far
        # (2026-07-29 weekday/weekend fix, oe3.forecast_explained) — every
        # other driver keeps the pre-existing Spanish-only `label`. Prefer
        # the EN variant when present+asked for; fall back to `label`
        # otherwise (unchanged behavior for hdd/cdd/weather).
        label = (c.get("label_en") if lang == "en" else None) or c["label"]
        detail = (c.get("detail_en") if lang == "en" else None) or c.get("detail")
        line = f"- driver: {label} | efecto_kwh: {c['phi']:+.2f}"
        if detail:
            line += f" | detalle: {detail}"
        lines.append(line)
    if alert:
        body_key = "body_en" if lang == "en" else "body_es"
        adv_key = "energy_advisory_en" if lang == "en" else "energy_advisory_es"
        lines.append(f"- aviso_meteorologico: {alert.get(body_key) or alert.get('body_es')} "
                    f"({alert.get(adv_key) or alert.get('energy_advisory_es')})")
    # Equipment caveat (hard requirement 2026-07-29) — defense layer 1: tell
    # the LLM up front which actions are off the table. `_scrub_equipment`
    # (defense layer 2, deterministic) is what actually enforces it.
    equipment = equipment or {}
    missing = []
    if not equipment.get("electric_heating", False):
        missing.append("electric heating" if lang == "en" else "calefacción eléctrica")
    if not equipment.get("electric_cooling", False):
        missing.append("electric AC/HVAC" if lang == "en" else "aire acondicionado/HVAC eléctrico")
    if missing:
        if lang == "en":
            lines.append(f"- equipment_missing: This household has NO {' or '.join(missing)} "
                         "— NEVER suggest actions for equipment it doesn't have.")
        else:
            lines.append(f"- equipo_ausente: Este hogar NO tiene {' ni '.join(missing)} "
                         "— NUNCA sugieras acciones sobre equipos que no tiene.")
    return "\n".join(lines)


class AdviceSkill(Skill):
    name = "advice"
    system_prompt = _SYSTEM

    def _template_fallback(self, entry: Dict[str, Any],
                           alert: Optional[Dict[str, Any]], lang: str,
                           equipment: Optional[Dict[str, Any]] = None) -> AdviceNarrative:
        """Deterministic degrade path — AI off / capped / unreachable. Still
        names the top driver and gives one generic shift tip, so the email
        never ships empty. `top` can never be a heating/cooling driver for a
        household lacking that equipment — oe3.forecast_explained already
        excludes it from `contributions` upstream — but the result still
        passes through `_scrub_equipment` for defense in depth."""
        attribution = entry.get("attribution") or {}
        top = (attribution.get("contributions") or [{}])[0]
        kwh = entry.get("kwh")
        eur = entry.get("eur")
        if lang == "en":
            summary = f"Forecast: {kwh} kWh" + (f" (~{eur} EUR)" if eur is not None else "") + \
                      (f" — mainly driven by {top.get('label')}." if top else ".")
            advice = ["Shift deferrable appliances (washer, dishwasher) to the cheapest hours of the day."]
        else:
            summary = f"Previsión: {kwh} kWh" + (f" (~{eur} €)" if eur is not None else "") + \
                      (f" — sobre todo por {top.get('label')}." if top else ".")
            advice = ["Adelanta lavadora/lavavajillas a las horas más baratas del día."]
        return _scrub_equipment(AdviceNarrative(summary=summary, advice=advice), equipment, entry, lang)

    async def narrate(self, entry: Dict[str, Any], alert: Optional[Dict[str, Any]] = None,
                      lang: str = "es",
                      equipment: Optional[Dict[str, Any]] = None) -> tuple[AdviceNarrative, int, int]:
        """Returns (narrative, prompt_tokens, completion_tokens). Tokens are
        0 when the AI path wasn't used (fallback) — callers should only log
        ai_usage.record() when tokens > 0 (a fallback isn't a billable call).

        `equipment` ({"electric_heating": bool, "electric_cooling": bool},
        from consumption.comfort_flex_for — hard requirement 2026-07-29) is
        passed to the LLM as an explicit caveat AND enforced deterministically
        via `_scrub_equipment` on EVERY return path below — the LLM's own
        discipline is never trusted alone to withhold a heating/cooling tip
        for equipment the household doesn't have."""
        if not ai_client.configured():
            return self._template_fallback(entry, alert, lang, equipment), 0, 0

        system = _SYSTEM_EN if lang == "en" else _SYSTEM
        facts = _facts_for_forecast(entry, alert, lang, equipment)
        lang_instruction = ("Reply STRICTLY in English (both summary and advice)."
                           if lang == "en" else
                           "Responde ESTRICTAMENTE en español (tanto summary como advice).")
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content":
                ("Datos de la previsión:\n" if lang != "en" else "Forecast data:\n") + facts +
                "\n\n" + lang_instruction +
                "\n\nDevuelve SOLO un objeto JSON {\"summary\": str, \"advice\": [str, ...]}."},
        ]
        got = await ai_client.llm_chat_full(messages, temperature=0.3, max_tokens=350)
        if not got or not got.get("text"):
            return self._template_fallback(entry, alert, lang, equipment), 0, 0
        usage = got.get("usage") or {}
        prompt_t = int(usage.get("prompt_tokens") or 0)
        completion_t = int(usage.get("completion_tokens") or 0)
        try:
            parsed = AdviceNarrative.model_validate_json(_strip_json(got["text"]))
            return _scrub_equipment(parsed, equipment, entry, lang), prompt_t, completion_t
        except (ValidationError, ValueError) as e:
            logger.warning("advice: JSON validation failed: %s", str(e)[:200])
            fb = self._template_fallback(entry, alert, lang, equipment)
            return fb, prompt_t, completion_t

    def _anomaly_template_fallback(self, entry: Dict[str, Any], lang: str,
                                   equipment: Optional[Dict[str, Any]] = None) -> AdviceNarrative:
        """Deterministic degrade path for anomaly narration — AI off/capped/
        unreachable. Direction-aware generic tip (never assumes UP when the
        deviation was actually DOWN), still passed through `_scrub_equipment`
        for defense in depth."""
        summary = _anomaly_deterministic_summary(entry, lang)
        observed, expected = entry.get("observed_w"), entry.get("expected_w")
        up = observed is not None and expected is not None and observed > expected
        if lang == "en":
            advice = ["Check whether an appliance was accidentally left running."] if up else \
                     ["Check whether a device lost power or was switched off unintentionally."]
        else:
            advice = ["Comprueba si algún electrodoméstico se ha quedado encendido sin querer."] if up else \
                     ["Comprueba que ningún aparato se haya quedado sin corriente o apagado sin querer."]
        return _scrub_equipment(AdviceNarrative(summary=summary, advice=advice), equipment, entry, lang,
                                fallback_summary=lambda: _anomaly_deterministic_summary(entry, lang))

    async def narrate_anomaly(self, entry: Dict[str, Any], lang: str = "es",
                              equipment: Optional[Dict[str, Any]] = None
                              ) -> tuple[AdviceNarrative, int, int]:
        """Anomaly counterpart to `narrate()` — same numbers-in/prose-out
        discipline and equipment guardrail, different (past-tense, non-
        alarmist) system prompt and fact shape (observed/expected W + z,
        not a kWh/€ forecast with SHAP contributions). Returns
        (narrative, prompt_tokens, completion_tokens)."""
        if not ai_client.configured():
            return self._anomaly_template_fallback(entry, lang, equipment), 0, 0

        system = _SYSTEM_ANOMALY_EN if lang == "en" else _SYSTEM_ANOMALY
        facts = _facts_for_anomaly(entry, lang, equipment)
        lang_instruction = ("Reply STRICTLY in English (both summary and advice)."
                           if lang == "en" else
                           "Responde ESTRICTAMENTE en español (tanto summary como advice).")
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content":
                ("Datos del evento anómalo:\n" if lang != "en" else "Anomaly event data:\n") + facts +
                "\n\n" + lang_instruction +
                "\n\nDevuelve SOLO un objeto JSON {\"summary\": str, \"advice\": [str, ...]}."},
        ]
        got = await ai_client.llm_chat_full(messages, temperature=0.3, max_tokens=300)
        if not got or not got.get("text"):
            return self._anomaly_template_fallback(entry, lang, equipment), 0, 0
        usage = got.get("usage") or {}
        prompt_t = int(usage.get("prompt_tokens") or 0)
        completion_t = int(usage.get("completion_tokens") or 0)
        try:
            parsed = AdviceNarrative.model_validate_json(_strip_json(got["text"]))
            narrative = _scrub_equipment(parsed, equipment, entry, lang,
                                         fallback_summary=lambda: _anomaly_deterministic_summary(entry, lang))
            return narrative, prompt_t, completion_t
        except (ValidationError, ValueError) as e:
            logger.warning("advice(anomaly): JSON validation failed: %s", str(e)[:200])
            fb = self._anomaly_template_fallback(entry, lang, equipment)
            return fb, prompt_t, completion_t
