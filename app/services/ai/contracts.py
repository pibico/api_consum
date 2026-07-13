"""ContractSkill — the LLM intelligence of the supply-contract domain.

Actions:
  · extract_tariff_sheet(md)  → ExtractedTariff  (populate the tariff catalog
    from a retailer's published tariff sheet — "recuperar la información de los
    distintos tipos de contrato"). Feed it the Markdown from `convert_client`.

Only public/aggregated text goes to AIDA — never the household's raw PII.
"""
from __future__ import annotations

from typing import Dict, Optional

from pydantic import BaseModel, Field

from app.services.ai.base import Skill


class ExtractedTariff(BaseModel):
    """One row of the tariff catalog, as read from a tariff sheet. Everything is
    optional — the model fills what the sheet states and leaves the rest null for
    a human to complete/confirm (never invents regulated values)."""
    retailer: Optional[str] = None
    product_name: Optional[str] = None
    contract_type: Optional[str] = Field(None, pattern="^(pvpc|fixed|indexed)$")
    access_tariff: Optional[str] = None   # 2.0TD / 3.0TD / 6.1TD… tal cual venga
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
    # Per-user fields — present on a SIGNED contract (not on a plain tariff
    # sheet), used to prefill the household's contract, not the catalog.
    cups: Optional[str] = None
    power_p1_kw: Optional[float] = Field(None, ge=0, le=15)
    power_p2_kw: Optional[float] = Field(None, ge=0, le=15)
    start_date: Optional[str] = None   # YYYY-MM-DD if determinable
    # Indexed component matrix (api_exo parity): {"CC":{"P1":..,"P2":..,"P3":..},
    # "ATR":{…}}. Only the contract-specific rows (CC, and ATR if itemized) —
    # the regulated defaults fill the rest server-side.
    components: Optional[Dict[str, Dict[str, float]]] = None
    # What the document IS. An INVOICE shows period-average prices that LOOK
    # fixed even on an indexed product → the UI must not trust contract_type
    # from a 'factura' and asks the user one plain question instead.
    document_kind: Optional[str] = Field(None, pattern="^(contrato|factura|ficha)$")
    # Invoice-only metadata — used to ARCHIVE the uploaded bill.
    billing_period_start: Optional[str] = None   # YYYY-MM-DD
    billing_period_end: Optional[str] = None     # YYYY-MM-DD
    total_eur: Optional[float] = Field(None, ge=0, lt=100000)
    confidence: Optional[float] = Field(None, ge=0, le=1)
    notes: Optional[str] = None


_EXTRACT_SYSTEM = (
    "Eres un experto en el mercado eléctrico doméstico español (tarifa de acceso "
    "2.0TD, periodos P1 punta / P2 llano / P3 valle). Recibes el texto (markdown) "
    "de una FICHA DE TARIFA de una comercializadora y devuelves un ÚNICO objeto "
    "JSON que describe ese producto para un catálogo.\n"
    "Reglas:\n"
    "- Los decimales pueden venir con COMA (0,149000): conviértelos a PUNTO "
    "(0.149). Precios de energía en €/kWh; término de potencia en €/kW·día.\n"
    "- Mapea periodos: Punta→P1, Llano→P2, Valle→P3.\n"
    "- access_tariff = la tarifa de acceso tal como aparezca (2.0TD, 3.0TD, "
    "6.1TD…), no la fuerces a 2.0TD.\n"
    "- EXTRAE TODOS los valores numéricos que aparezcan en el texto. Si un precio "
    "está escrito, NO lo dejes en null; null es SOLO para lo que no aparece.\n"
    "- MARGEN del comercializador: en un contrato indexado suele llamarse "
    "'Coste comercialización', 'Coste de comercialización' o 'CC' (aparece en la "
    "fórmula como CC). Es margin_eur_kwh. Si viene POR PERIODOS (P1..P6) con "
    "valores distintos (p.ej. 'P1 0,03 P2 0,03 P3 0,08'), pon el de P1 en "
    "margin_eur_kwh y anota los demás periodos en 'notes'. También puede llamarse "
    "'coste de gestión' o 'FEE'; si viene en €/MWh divídelo entre 1000.\n"
    "- PEAJES/CARGOS: 'ATR', 'Peajes de acceso y cargos', 'Término de Energía' "
    "regulado → passthru_p1/p2/p3 si traen valor numérico por periodo.\n"
    "- 'Servicio de gestión' o cuota fija en €/día o €/mes → meter_rental_eur_month "
    "o other en €/mes (si es €/día multiplícalo por 30).\n"
    "- Si es INDEXADO y el Coste comercialización (CC) viene POR PERIODO, ponlo "
    "TAMBIÉN en 'components' así: {\"CC\": {\"P1\": 0.06, \"P2\": 0.04, \"P3\": "
    "0.03}}. Si ves ATR/peajes por periodo con valor, añade \"ATR\" igual. NO "
    "rellenes SA/CR/DSV/PP/IM/BS (se completan por defecto).\n"
    "- retailer = nombre de la comercializadora (aunque venga junto al producto, "
    "p.ej. 'Plan Estable - EnergiaCo' → retailer 'EnergiaCo').\n"
    "- contract_type: 'pvpc' si es el PVPC regulado; 'fixed' si el precio de "
    "energía es fijo; 'indexed' si es OMIE/pool + un margen.\n"
    "- En indexado, 'margin_eur_kwh' es el margen del comercializador y "
    "'passthru_*' son peajes+cargos si la ficha los detalla; si no, null.\n"
    "- Si es un CONTRATO FIRMADO (no una ficha), extrae también los datos del "
    "suministro: 'cups' (código CUPS, empieza por ES), 'power_p1_kw' y "
    "'power_p2_kw' (potencia contratada en kW; en 2.0TD suele haber UN valor — "
    "si solo aparece uno, ponlo en ambos), y 'start_date' = fecha de inicio o "
    "firma en formato YYYY-MM-DD (convierte '6 de Agosto de 2025' → "
    "'2025-08-06').\n"
    "- NO inventes valores regulados ni redondees a tu criterio.\n"
    "- 'document_kind': clasifica el documento — 'contrato' (condiciones "
    "particulares/firmado), 'factura' (tiene periodo de facturación, lecturas, "
    "importe total) o 'ficha' (ficha comercial de tarifa). OJO: en una FACTURA "
    "los €/kWh por periodo son PROMEDIOS del mes — si el documento es factura, "
    "extráelos igualmente pero NO deduzcas contract_type de ellos (déjalo null "
    "salvo que la factura diga explícitamente 'indexado'/'PVPC'/'precio fijo').\n"
    "- En una factura, el CUPS, la potencia contratada, la comercializadora y "
    "los impuestos (IVA %, impuesto eléctrico %) sí son fiables: extráelos. "
    "Extrae también 'billing_period_start'/'billing_period_end' (el periodo de "
    "consumo/facturación, YYYY-MM-DD; '27/02/2026' → '2026-02-27') y "
    "'total_eur' (importe total de la factura en euros).\n"
    "- 'confidence' 0..1 = tu seguridad global; BÁJALA si faltan precios clave.\n"
    "\nIMPORTANTE: al convertir un PDF, la tabla puede quedar APLANADA en una "
    "línea con el periodo y su precio juntos. Ejemplos:\n"
    "  'Punta (P1) 0,149000 Llano (P2) 0,120000 Valle (P3) 0,085000'  →  "
    "energy_p1_eur_kwh=0.149, energy_p2_eur_kwh=0.120, energy_p3_eur_kwh=0.085\n"
    "  'Punta 0,100000 Valle 0,010000' (bajo potencia)  →  "
    "power_p1_eur_kw_day=0.100, power_p2_eur_kw_day=0.010\n"
    "Asocia cada número al periodo que lo precede, aunque estén en la misma línea.\n"
    "Responde SOLO con el objeto JSON, sin texto ni ```."
)


class ContractSkill(Skill):
    name = "contract"
    system_prompt = _EXTRACT_SYSTEM

    async def extract_tariff_sheet(self, markdown: str) -> Optional[ExtractedTariff]:
        """Tariff-sheet Markdown → structured catalog row (for human review)."""
        if not markdown or not markdown.strip():
            return None
        prompt = ("Documento (ficha de tarifa o contrato firmado), en markdown:\n\n" +
                  markdown.strip()[:18000] +
                  "\n\nExtrae al objeto JSON pedido: el producto/tarifa, TODOS los "
                  "precios numéricos que veas (coma decimal → punto) y, si es un "
                  "contrato firmado, también CUPS, potencia contratada y fecha.")
        # temperature 0 → determinista (evita que se pierda el margen entre corridas)
        return await self.run_json(prompt, ExtractedTariff, temperature=0.0,
                                   max_tokens=1400)
