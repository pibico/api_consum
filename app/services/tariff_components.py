"""Indexed-tariff price components — server-side port of api_exo's tariff editor.

api_exo has the component matrix + formula but ONLY in the browser (localStorage,
`apiexo.components.v1`). CONSUM-IA needs it PERSISTED on the contract so the
indexed price is costed server-side. This module is the single source of truth
for the component model, shared by pricing (cost) and the contract UI/extraction.

Components (per band P1/P2/P3 for 2.0TD), €/kWh unless a percent:
  PMD  Pool price — LIVE from OMIE per hour (not stored here, passed in)
  SA   Adjustment services (RD 446/2023) · BOE
  CR   Regulated components (Orden TED/1487/2024) · BOE
  DSV  Portfolio deviation · BOE
  PP   Loss coefficient (PERCENT, stored as decimal) · REE
  CC   Commercialization cost / margin (PER PERIOD) · contract-specific
  IM   Municipal tax (PERCENT, stored as decimal) · ayuntamiento
  ATR  Access tolls & charges (peajes) · BOE · per access tariff
  BS   Social bonus financing · BOE

Formula (api_exo parity):
  Final = [(PMD + SA + CR + DSV) × (1 + PP) + CC] × (1 + IM) + ATR + BS
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# Editable component ids (PMD is live from OMIE, never stored).
COMPONENT_IDS: List[str] = ["SA", "CR", "DSV", "PP", "CC", "IM", "ATR", "BS"]
PERCENT_IDS = {"PP", "IM"}   # stored as decimal (0.03 = 3 %)

UNIT = {c: ("%" if c in PERCENT_IDS else "EUR/kWh") for c in COMPONENT_IDS}
SOURCE = {"SA": "BOE", "CR": "BOE", "DSV": "BOE", "PP": "REE", "CC": "contract",
          "IM": "ayuntamiento", "ATR": "BOE", "BS": "BOE"}


def bands_for(access_tariff: str) -> List[str]:
    return ["P1", "P2", "P3"] if access_tariff == "2.0TD" else \
        ["P1", "P2", "P3", "P4", "P5", "P6"]


def default_components(access_tariff: str = "2.0TD") -> Dict[str, Dict[str, float]]:
    """BOE/REE reference defaults (byte-parity with api_exo defaultComponents)."""
    t20 = access_tariff == "2.0TD"
    base: Dict[str, Dict[str, float]] = {
        "SA":  {"P1": 0.0042, "P2": 0.0038, "P3": 0.0035, "P4": 0.0030, "P5": 0.0025, "P6": 0.0020},
        "CR":  {"P1": 0.0161, "P2": 0.0102, "P3": 0.0039, "P4": 0.0030, "P5": 0.0020, "P6": 0.0010},
        "DSV": {"P1": 0.0021, "P2": 0.0021, "P3": 0.0021, "P4": 0.0021, "P5": 0.0021, "P6": 0.0021},
        "PP":  {"P1": 0.03, "P2": 0.03, "P3": 0.03, "P4": 0.03, "P5": 0.03, "P6": 0.03},
        "ATR": ({"P1": 0.0275, "P2": 0.0167, "P3": 0.0010} if t20 else
                {"P1": 0.02150, "P2": 0.01614, "P3": 0.00872, "P4": 0.00464, "P5": 0.00139, "P6": 0.00084}),
        "CC":  ({"P1": 0.0600, "P2": 0.0400, "P3": 0.0300} if t20 else
                {"P1": 0.0350, "P2": 0.0300, "P3": 0.0250, "P4": 0.0200, "P5": 0.0150, "P6": 0.0100}),
        "IM":  {"P1": 0.01051, "P2": 0.01051, "P3": 0.01051, "P4": 0.01051, "P5": 0.01051, "P6": 0.01051},
        "BS":  {"P1": 0.00378, "P2": 0.00378, "P3": 0.00378, "P4": 0.00378, "P5": 0.00378, "P6": 0.00378},
    }
    bands = bands_for(access_tariff)
    return {cid: {b: vals[b] for b in bands} for cid, vals in base.items()}


def base_from_exo(payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Dict[str, float]]]:
    """api_exo GET /tariffs/components payload → {cid: {band: value}} for the
    formula components (skips scalar IVA/IEE — invoice-level taxes)."""
    comps = (payload or {}).get("components") or {}
    out: Dict[str, Dict[str, float]] = {}
    for cid in COMPONENT_IDS:
        bands = (comps.get(cid) or {}).get("bands")
        if isinstance(bands, dict) and bands:
            try:
                out[cid] = {b: float(v) for b, v in bands.items() if v is not None}
            except (TypeError, ValueError):
                continue
    return out or None


def merged(access_tariff: str, overrides: Optional[Dict[str, Any]],
           base: Optional[Dict[str, Dict[str, float]]] = None) -> Dict[str, Dict[str, float]]:
    """Three layers: vendored defaults ← api_exo SSOT base ← the contract's own
    stored components (CC and anything the user/extraction set). `overrides` is
    the contract's persisted JSONB; `base` comes from base_from_exo()."""
    data = default_components(access_tariff)
    for layer in (base, overrides):
        if not layer:
            continue
        for cid in COMPONENT_IDS:
            ov = layer.get(cid)
            if isinstance(ov, dict):
                for b, v in ov.items():
                    if b in data.get(cid, {}) and v is not None:
                        try:
                            data[cid][b] = float(v)
                        except (TypeError, ValueError):
                            pass
    return data


def final_price_eur_kwh(components: Dict[str, Dict[str, float]], band: str,
                        pmd_eur_kwh: float) -> float:
    """Final indexed €/kWh for one band, given the live PMD (OMIE) in €/kWh.
    Final = [(PMD + SA + CR + DSV) × (1 + PP) + CC] × (1 + IM) + ATR + BS."""
    def g(k: str) -> float:
        row = components.get(k) or {}
        v = row.get(band)
        return float(v) if v is not None else 0.0
    e = (pmd_eur_kwh + g("SA") + g("CR") + g("DSV")) * (1 + g("PP")) + g("CC")
    return e * (1 + g("IM")) + g("ATR") + g("BS")
