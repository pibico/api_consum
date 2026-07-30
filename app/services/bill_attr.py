"""bill_attr.py — Phase 2.5 B2: the €-delta waterfall for ONE invoice.

`attribute_bill(actual, expected)` decomposes `actual_total_eur -
expected_total_eur` into signed, labelled drivers so the member (and the
email) can read WHICH part moved: weather, calendar, unexplained
consumption, price/tariff, contracted power, fixed charges, taxes — plus
whatever residual is left when a component isn't fully knowable (uploaded
bills without a clean AI extraction).

EXACTNESS (spec requirement: "Σ drivers + expected == actual €"): every
component below follows the SAME two-branch rule —
  - expected value KNOWN  → phi = actual - expected (contributes to base
    via `expected`, drives the delta)
  - expected value UNKNOWN → phi = 0, `expected`'s share of `base` is
    silently set to the ACTUAL value instead (a pass-through — "nothing to
    compare, so don't invent a discrepancy"). Either way
    base_component + phi_component == actual_component ALWAYS, so summing
    over every component (energy split further into weather/calendar/
    consumption/price sub-drivers, still additive) preserves the identity
    exactly. A final `residual` line absorbs any leftover cents (only
    non-zero for UPLOADED bills, whose AI-extracted component amounts don't
    perfectly foot to the bill's own printed total) so the identity holds
    to the cent even then.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def _passthrough(actual: Optional[float], expected: Optional[float]) -> tuple:
    """(base_share, phi) for a plain (non-energy) component: phi=0 and
    base=actual when `expected` isn't knowable, else phi=actual-expected
    and base=expected. Either branch: base+phi == actual exactly."""
    if actual is None:
        actual = 0.0
    if expected is None:
        return actual, 0.0
    return expected, round(actual - expected, 4)


def attribute_bill(actual: Dict[str, Any], expected: Dict[str, Any]) -> Dict[str, Any]:
    """`actual` = bill_expectation.components_for(invoice) of the invoice
    under test (its `total_eur` is the ONLY number treated as absolute
    ground truth). `expected` = bill_expectation.expected_bill(...)'s
    return value.

    Returns {actual_total_eur, base, drivers:[{label, phi, unit, detail}],
    residual}, with base + sum(d.phi for d in drivers) + residual ==
    actual_total_eur EXACTLY (asserted by the caller's verification, not
    just claimed — see debug/api_consum_debug/ B2 check)."""
    actual_total = float(actual.get("total_eur") or 0.0)
    drivers: List[Dict[str, Any]] = []
    base = 0.0

    # ── Energy: weather/calendar/consumption/price split when both sides
    # know the kWh; else a single pass-through "energía" driver. ──────────
    ekwh_a = actual.get("energy_kwh")
    ekwh_e = expected.get("expected_kwh")
    price_e = expected.get("price_expected_eur_kwh")
    eeur_a = actual.get("energy_eur")
    eeur_e = expected.get("expected_energy_eur")
    attribution = expected.get("attribution")

    if ekwh_a is not None and ekwh_e is not None and price_e is not None \
            and eeur_a is not None and attribution is not None:
        base_kwh = attribution["base"]
        base += base_kwh * price_e
        for c in attribution["contributions"]:
            phi_eur = round(c["phi"] * price_e, 4)
            if abs(phi_eur) < 0.005:
                continue
            drivers.append({"label": c["label"], "phi": phi_eur, "unit": "€",
                            "detail": f"{c['phi']:+.2f} kWh"})
        consumption_resid_kwh = ekwh_a - ekwh_e
        price_actual_mean = (eeur_a / ekwh_a) if ekwh_a else None
        if price_actual_mean is not None:
            phi_consumption = round(consumption_resid_kwh * price_e, 4)
            phi_price = round(ekwh_a * (price_actual_mean - price_e), 4)
            if abs(phi_consumption) >= 0.005:
                drivers.append({"label": "Consumo (no explicado por clima/calendario)",
                                "phi": phi_consumption, "unit": "€",
                                "detail": f"{consumption_resid_kwh:+.1f} kWh sobre lo esperado"})
            if abs(phi_price) >= 0.005:
                drivers.append({"label": "Precio/tarifa de la energía", "phi": phi_price,
                                "unit": "€",
                                "detail": f"{price_actual_mean:.4f} vs {price_e:.4f} €/kWh esperado"})
        else:
            phi_catchall = round(eeur_a - (base_kwh * price_e
                                           + sum(c["phi"] * price_e for c in attribution["contributions"])), 4)
            if abs(phi_catchall) >= 0.005:
                drivers.append({"label": "Energía (consumo + precio)", "phi": phi_catchall, "unit": "€",
                                "detail": None})
    else:
        eb, phi = _passthrough(eeur_a, eeur_e)
        base += eb
        if abs(phi) >= 0.005:
            drivers.append({"label": "Energía (sin desglose de kWh para comparar)"
                            if eeur_e is not None else "Energía (sin histórico para comparar)",
                            "phi": phi, "unit": "€", "detail": None})

    # ── Power / fixed / tax: plain pass-through drivers (spec: potencia is
    # NEVER folded into the energy/weather attribution). ──────────────────
    for label, key_a, key_e in (
        ("Potencia contratada", "power_eur", "expected_power_eur"),
        ("Cargos fijos (alquiler de contador, etc.)", "fixed_eur", "expected_fixed_eur"),
        ("Impuestos (IEE + IVA)", "tax_eur", "expected_tax_eur"),
    ):
        eb, phi = _passthrough(actual.get(key_a), expected.get(key_e))
        base += eb
        if abs(phi) >= 0.005:
            drivers.append({"label": label, "phi": phi, "unit": "€", "detail": None})

    drivers.sort(key=lambda d: -abs(d["phi"]))
    # Round `base` (and re-round each driver's phi, already ~4dp) to the
    # SAME 2-decimal precision reported to the caller BEFORE computing the
    # residual against `actual_total` — computing it against the raw,
    # unrounded accumulator would leave a few-cent gap between the
    # ROUNDED numbers actually returned (base + drivers + residual) and
    # actual_total_eur, breaking the exact-identity guarantee for anyone
    # summing the RETURNED values (as the spec's acceptance test does).
    base_r = round(base, 2)
    for d in drivers:
        d["phi"] = round(d["phi"], 2)
    residual = round(actual_total - (base_r + sum(d["phi"] for d in drivers)), 2)
    if abs(residual) >= 0.01:
        drivers.append({"label": "Otros / ajuste de redondeo", "phi": residual, "unit": "€",
                        "detail": "Diferencia entre el desglose estimado y el importe real de la factura."})
        residual_reported = 0.0
    else:
        residual_reported = residual

    return {"actual_total_eur": round(actual_total, 2), "base": base_r,
            "drivers": drivers, "residual": round(residual_reported, 2),
            "delta_eur": round(actual_total - (expected.get("expected_total_eur")
                                               if expected.get("expected_total_eur") is not None
                                               else base_r), 2)}
