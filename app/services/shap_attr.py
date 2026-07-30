"""Deterministic Shapley attribution (S2) — exact for linear (OLS) models.

For y_hat = b0 + sum(b_i * x_i), the exact Shapley value of feature i on a
single prediction, relative to the training-set means, is:

    phi_i = b_i * (x_i - x_bar_i)

and the "base value" (prediction at the mean feature vector — the model's
unconditional expectation) is:

    base = b0 + sum(b_i * x_bar_i)

so that `base + sum(phi_i) == y_hat` EXACTLY (the efficiency property SHAP
guarantees for any model — here it falls out of plain algebra, no shap lib
needed). No approximation, no sampling: this is closed-form because the
model is linear. Ported straight from the `2026-07-28_explainable_ai_*`
design doc §6. The LLM (AdviceSkill) narrates these numbers; it never
computes or overrides them.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence


def shapley_linear(coefs: Dict[str, float], x: Dict[str, float],
                   x_bar: Dict[str, float]) -> Dict[str, float]:
    """phi_i = coefs[i] * (x[i] - x_bar[i]) for every feature in `coefs`.
    Missing x/x_bar entries default to 0.0 (phi=0 — a feature that isn't
    present contributes nothing, rather than raising)."""
    return {
        feat: beta * (float(x.get(feat, 0.0)) - float(x_bar.get(feat, 0.0)))
        for feat, beta in coefs.items()
    }


def base_value(intercept: float, coefs: Dict[str, float],
               x_bar: Dict[str, float]) -> float:
    """f(mean feature vector) — the model's unconditional expectation."""
    return intercept + sum(beta * float(x_bar.get(feat, 0.0))
                           for feat, beta in coefs.items())


def attribute_forecast(intercept: float, coefs: Dict[str, float],
                       x: Dict[str, float], x_bar: Dict[str, float],
                       labels: Optional[Dict[str, str]] = None,
                       units: Optional[Dict[str, str]] = None
                       ) -> Dict[str, Any]:
    """Full attribution bundle for one forecast: {base, contributions, total}.

    `contributions` is sorted by |phi| descending (biggest driver first — what
    the narrative/email leads with). `total` == base + sum(phi), by
    construction (the efficiency property, asserted by the S2 unit check).
    """
    labels = labels or {}
    units = units or {}
    phi = shapley_linear(coefs, x, x_bar)
    base = base_value(intercept, coefs, x_bar)
    contributions: List[Dict[str, Any]] = []
    for feat, value in sorted(phi.items(), key=lambda kv: -abs(kv[1])):
        contributions.append({
            "feature": feat,
            "label": labels.get(feat, feat),
            "phi": round(value, 4),
            "x": round(float(x.get(feat, 0.0)), 4),
            "x_bar": round(float(x_bar.get(feat, 0.0)), 4),
            "unit": units.get(feat, ""),
        })
    total = base + sum(phi.values())
    return {"base": round(base, 4), "contributions": contributions,
           "total": round(total, 4)}


def residual_bucket(observed: float, expected_total: float) -> float:
    """observed - (base + sum(phi)) — the UNEXPLAINED part. Non-zero here
    means the linear model's drivers don't fully account for the observed
    value (Phase 2's anomaly-worthiness signal); Phase 1 only ever compares a
    forecast to itself (residual == 0 by construction) but the helper is kept
    here so Phase 2 can reuse the exact same attribution math unmodified."""
    return observed - expected_total
