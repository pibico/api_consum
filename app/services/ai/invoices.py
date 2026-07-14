"""InvoiceSkill — plain-language intelligence of the invoice domain (phase 2).

First action live: `explain_invoice_plain` — "tu factura, explicada": what you
pay, why, how it compares with the previous one, and one simple tip. Reuses the
skills foundation verbatim (Skill.run_text, require_ai gate, aggregates-only —
no PII beyond the retailer name ever reaches AIDA).

`facts_for_invoice()` is the input contract: it flattens a stored invoice row
(generated OR uploaded) into the aggregated, jargon-free facts the prompt gets.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from app.services.ai.base import Skill

_EXPLAIN_SYSTEM = (
    "Eres el asistente de CONSUM-IA. Explica una factura de la luz a una "
    "persona mayor que no entiende de electricidad, con calidez y SIN jerga, "
    "pero EN PROFUNDIDAD: que al terminar entienda de verdad por qué paga lo "
    "que paga y qué es cada pieza de la factura.\n"
    "Estructura la respuesta en bloques cortos, cada uno con su titulillo en "
    "negrita entre ** **, en este orden (omite un bloque solo si no aplica):\n"
    "**Lo que pagas** — el total y el periodo, en una frase.\n"
    "**La energía que has usado** — es lo que gastas al usar los aparatos; da "
    "los kWh y su coste si los tienes. Si hay datos por franjas, explica que "
    "la luz tiene horas caras (mañana y tarde-noche de lunes a viernes), horas "
    "normales y horas baratas (noche y fines de semana), y reparte su consumo.\n"
    "**La potencia contratada** — el fijo que se paga aunque no se gaste nada: "
    "son los kW que tu casa puede usar a la vez (como el grosor de una "
    "tubería: más gruesa, más caro el alquiler). Da sus kW y el coste si lo "
    "tienes.\n"
    "**Los peajes y cargos** — explica que DENTRO del precio hay una parte "
    "regulada por el Estado que no es de la compañía: paga las redes que "
    "llevan la luz hasta casa (los 'peajes') y otros costes del sistema "
    "eléctrico (los 'cargos'). Se pagan igual con cualquier compañía. Da la "
    "cifra solo si te la doy.\n"
    "**El bono social** — una pequeña aportación solidaria, obligatoria por "
    "ley, con la que todos ayudamos a pagar la luz de hogares vulnerables. "
    "Suelen ser unos céntimos.\n"
    "**Los impuestos** — el impuesto especial de la electricidad (un "
    "porcentaje pequeño) y el IVA sobre casi todo lo anterior. Da las cifras "
    "si las tienes.\n"
    "**Comparada con tu factura anterior** — solo si te doy datos: si sube o "
    "baja, cuánto, y una causa probable.\n"
    "Termina con UN consejo sencillo (p. ej. mover lavadora/lavavajillas a "
    "horas baratas) o un ánimo breve.\n"
    "REGLAS DURAS: nada de siglas ni jerga (jamás P1/P2/P3, IEE, ATR, término "
    "de energía…); NO inventes cifras — si no te doy un importe, explica el "
    "concepto sin números; euros con coma decimal y símbolo €; frases cortas.\n"
    "FORMATO: SOLO texto corrido y los titulillos entre ** **. PROHIBIDO usar "
    "tablas, líneas con «|», separadores «---», encabezados «#» o cualquier "
    "otro Markdown — se lee en un panel sencillo y lo usan personas mayores."
)


def facts_for_invoice(inv: Dict[str, Any],
                      prev: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Aggregated, jargon-free facts from a stored invoice row (works for both
    generated settlements and uploaded retailer bills)."""
    bd = inv.get("breakdown") or {}
    facts: Dict[str, Any] = {
        "periodo": f"{inv.get('period_start')} a {inv.get('period_end')}",
        "importe_total_eur": inv.get("total_eur"),
    }
    if inv.get("origin") == "uploaded" or bd.get("uploaded"):
        ex = bd.get("extracted") or {}
        facts.update({
            "tipo_documento": "factura real de la comercializadora (subida por el usuario)",
            "comercializadora": ex.get("retailer"),
            "potencia_contratada_kw": ex.get("power_p1_kw"),
            "precio_horas_caras_eur_kwh": ex.get("energy_p1_eur_kwh"),
            "precio_horas_normales_eur_kwh": ex.get("energy_p2_eur_kwh"),
            "precio_horas_baratas_eur_kwh": ex.get("energy_p3_eur_kwh"),
        })
        # The full converted bill text (stored at upload / lazily backfilled)
        # lets the explanation use the REAL line items — peajes, bono social,
        # taxes, meter rental — instead of concept-only prose.
        md = bd.get("markdown")
        if md:
            facts["texto_completo_de_la_factura"] = (
                "(usa los importes reales de aquí; ignora datos personales)\n"
                + md[:12000])
    else:
        segs = bd.get("segments") or []
        per = {"P1": {"kwh": 0.0, "eur": 0.0}, "P2": {"kwh": 0.0, "eur": 0.0},
               "P3": {"kwh": 0.0, "eur": 0.0}}
        retailer = None
        for s in segs:
            for p, v in (s.get("energy") or {}).items():
                if p in per and isinstance(v, dict):
                    per[p]["kwh"] += v.get("kwh") or 0
                    per[p]["eur"] += v.get("eur") or 0
            c = s.get("contract") or {}
            retailer = retailer or c.get("retailer")
        taxes = round((inv.get("iee_eur") or 0) + (inv.get("vat_eur") or 0), 2)
        facts.update({
            "tipo_documento": "factura calculada por CONSUM-IA con el consumo medido",
            "comercializadora": retailer,
            "energia_usada_kwh": inv.get("energy_kwh"),
            "coste_energia_eur": inv.get("energy_eur"),
            "coste_potencia_eur": inv.get("power_eur"),
            "otros_fijos_eur": inv.get("fixed_eur"),
            "impuestos_eur": taxes,
            "kwh_en_horas_caras": round(per["P1"]["kwh"], 1),
            "kwh_en_horas_normales": round(per["P2"]["kwh"], 1),
            "kwh_en_horas_baratas": round(per["P3"]["kwh"], 1),
        })
    if prev:
        facts["factura_anterior_periodo"] = (
            f"{prev.get('period_start')} a {prev.get('period_end')}")
        facts["factura_anterior_total_eur"] = prev.get("total_eur")
        if prev.get("energy_kwh"):
            facts["factura_anterior_kwh"] = prev.get("energy_kwh")
    return facts


class BillAmounts(BaseModel):
    """Structured line-item amounts of a bill — feeds the ILLUSTRATIVE visuals
    (stacked cost bar, per-band energy cards). All optional: only what the
    document states, never invented."""
    energia_eur: Optional[float] = Field(None, ge=0, lt=100000)
    potencia_eur: Optional[float] = Field(None, ge=0, lt=10000)
    peajes_eur: Optional[float] = Field(None, ge=0, lt=10000)
    bono_social_eur: Optional[float] = Field(None, ge=0, lt=100)
    impuestos_eur: Optional[float] = Field(None, ge=0, lt=10000)
    alquiler_eur: Optional[float] = Field(None, ge=0, lt=100)
    kwh_horas_caras: Optional[float] = Field(None, ge=0, lt=100000)
    kwh_horas_normales: Optional[float] = Field(None, ge=0, lt=100000)
    kwh_horas_baratas: Optional[float] = Field(None, ge=0, lt=100000)
    precio_horas_caras: Optional[float] = Field(None, ge=0, lt=10)
    precio_horas_normales: Optional[float] = Field(None, ge=0, lt=10)
    precio_horas_baratas: Optional[float] = Field(None, ge=0, lt=10)


_AMOUNTS_SYSTEM = (
    "Extraes importes de una factura de la luz española a un objeto JSON. "
    "Mapea: término/coste de energía→energia_eur; término/coste de potencia→"
    "potencia_eur; peajes de acceso y cargos SOLO los de energía (si el peaje "
    "de potencia ya está dentro del coste de potencia, NO lo cuentes otra vez; "
    "la suma de todos los importes debe aproximarse al total de la factura)→"
    "peajes_eur; bono social→bono_social_eur; impuesto eléctrico + IVA sumados→"
    "impuestos_eur; alquiler de contador/equipos→alquiler_eur.\n"
    "Los kWh POR PERIODO están casi siempre en la tabla de consumos o lecturas "
    "(columnas P1/P2/P3 o punta/llano/valle): P1/punta→kwh_horas_caras, "
    "P2/llano→kwh_horas_normales, P3/valle→kwh_horas_baratas — extráelos "
    "SIEMPRE que aparezcan, junto a sus precios €/kWh.\n"
    "CUIDADO con el desglose 'X kWh x Y €/kWh': el kWh de la franja es X (el "
    "número ANTES de 'kWh'), NUNCA el importe resultante en € (X·Y). Ejemplo: "
    "'P1 (Punta): 46,00 kWh x 0,083647 €/kWh' → kwh_horas_caras=46.0 y "
    "precio_horas_caras=0.083647 (no 3.85, que es el importe). Si la factura "
    "declara el consumo total del periodo, la suma P1+P2+P3 debe aproximarse "
    "a ese total — si no cuadra, revisa que no hayas cogido importes en €.\n"
    "Coma decimal → punto. null para lo que NO aparezca — no inventes NADA. "
    "Responde SOLO el objeto JSON."
)


_QA_SYSTEM = (
    "Eres el asistente de CONSUM-IA. Respondes UNA duda concreta de una "
    "persona mayor sobre su factura de la luz, con calidez y SIN jerga.\n"
    "REGLAS DURAS: responde SOLO sobre esta factura, la electricidad del "
    "hogar o su tarifa; si la pregunta no va de eso, dilo amablemente en una "
    "frase y no respondas otra cosa. Nada de siglas ni jerga (jamás P1/P2/P3, "
    "IEE, ATR, término de energía…): di 'horas caras/normales/baratas', 'el "
    "fijo de la potencia', 'los peajes', 'los impuestos'. NO inventes cifras "
    "— usa SOLO los datos que te doy; si no tengo el dato, dilo y explica el "
    "concepto. Euros con coma decimal y símbolo €.\n"
    "FORMATO: 2 a 6 frases cortas de texto corrido; puedes marcar los "
    "importes clave entre ** **. PROHIBIDO listas, tablas, «|», «---», "
    "encabezados o cualquier otro Markdown."
)


class AnomalyNarrative(BaseModel):
    """Plain-language wrapper around DETERMINISTIC anomaly signals — the LLM
    only phrases what the numbers already say; it never decides the verdict."""
    headline: str = Field(..., max_length=200)     # one plain sentence
    causes: List[str] = Field(default_factory=list, max_length=4)
    advice: Optional[str] = Field(None, max_length=300)


_ANOMALY_SYSTEM = (
    "Redactas avisos de facturas de la luz para una persona mayor, en español "
    "claro y SIN jerga. Te doy señales YA CALCULADAS (desviaciones en % y en "
    "€/día frente a sus facturas anteriores). Tu trabajo es SOLO redactarlas: "
    "headline = una frase clara con la cifra clave; causes = hasta 3 causas "
    "probables coherentes con las señales (más consumo, más horas caras, "
    "precio más alto, más días de periodo…); advice = un consejo sencillo si "
    "procede. NO inventes números que no te dé. NO cambies el veredicto. "
    "Responde SOLO el objeto JSON."
)


class InvoiceSkill(Skill):
    name = "invoice"
    system_prompt = _EXPLAIN_SYSTEM

    async def explain_invoice_plain(self, facts: Dict[str, Any]) -> Optional[str]:
        """Aggregated facts → a warm, jargon-free, concept-deep explanation
        (or None: AI off)."""
        lines = [f"- {k}: {v}" for k, v in facts.items() if v is not None]
        if not lines:
            return None
        return await self.run_text(
            "Datos agregados de la factura (no muestres los nombres técnicos "
            "de los campos; lo que no tenga dato, explícalo como concepto sin "
            "cifras):\n" + "\n".join(lines),
            temperature=0.3, max_tokens=950)

    async def answer_billing_question(self, question: str,
                                      facts: Dict[str, Any]) -> Optional[str]:
        """One plain-language answer to the user's question about THIS invoice,
        grounded on its aggregated facts (or None: AI off/unavailable)."""
        question = (question or "").strip()[:300]
        if not question:
            return None
        lines = [f"- {k}: {v}" for k, v in facts.items() if v is not None]
        return await self.run_text(
            "Datos de la factura (no muestres los nombres técnicos de los "
            "campos):\n" + "\n".join(lines) +
            "\n\nPregunta del usuario: " + question +
            "\n\nRespóndela en llano.",
            system=_QA_SYSTEM, temperature=0.3, max_tokens=450)

    async def extract_bill_amounts(self, markdown: str) -> Optional[BillAmounts]:
        """Bill markdown → structured line-item amounts (deterministic, t=0)."""
        if not markdown or not markdown.strip():
            return None
        return await self.run_json(
            "Factura (markdown):\n\n" + markdown.strip()[:15000] +
            "\n\nExtrae los importes al objeto JSON pedido.",
            BillAmounts, system=_AMOUNTS_SYSTEM, temperature=0.0, max_tokens=500)

    async def narrate_anomaly(self, signals: Dict[str, Any]) -> Optional[AnomalyNarrative]:
        """Computed deviation signals → plain-language narrative (JSON)."""
        lines = [f"- {k}: {v}" for k, v in signals.items() if v is not None]
        return await self.run_json(
            "Señales calculadas de esta factura frente a su histórico:\n" +
            "\n".join(lines), AnomalyNarrative, system=_ANOMALY_SYSTEM,
            temperature=0.2, max_tokens=400)
