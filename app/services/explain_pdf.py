"""Branded "Tu factura, explicada" PDF (WeasyPrint) — printable hand-out.

render(invoice, explanation_text, anomaly) → PDF bytes. Same branding machinery
as invoice_pdf.py (Montserrat/Inter TTFs, inline pibiCo logo, A4). The LLM
explanation arrives as plain text with **bold** mini-headers per concept; it is
parsed into blocks here and laid out as a compact two-column card grid.
Synchronous & CPU-bound — call via asyncio.to_thread.
"""
from __future__ import annotations

import os
import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

_BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_TEMPLATES = os.path.join(_BASE, "templates")
_FONTS = os.path.join(_BASE, "static", "fonts", "pdf")
_LOGO = os.path.join(_BASE, "static", "icons", "pibiCo_logo_new.svg")

_env = Environment(
    loader=FileSystemLoader(_TEMPLATES),
    autoescape=select_autoescape(["html", "xml"]),
)


def _eur(v: Any) -> str:
    try:
        return f"{float(v):,.2f} €".replace(",", "·").replace(".", ",").replace("·", ".")
    except (TypeError, ValueError):
        return "—"


def _inline(text: str) -> Markup:
    """Escape + inline **bold** → <b> (amounts inside the prose)."""
    html = str(escape(text.strip()))
    html = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", html)
    return Markup(html.replace("\n", "<br>"))


_TITLE_RE = re.compile(r"^\*\*(.{2,70}?)\*\*[ \t]*$")


def _parse_blocks(text: str):
    """Split the LLM text into (intro, [{title, html}], closing). A line that
    is ONLY a **bold** phrase starts a block; inline bolds stay in the prose.
    A final untitled paragraph after the blocks becomes the closing note."""
    intro_lines: List[str] = []
    blocks: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for raw in text.split("\n"):
        m = _TITLE_RE.match(raw.strip())
        if m:
            cur = {"title": m.group(1).strip(), "lines": []}
            blocks.append(cur)
            continue
        (cur["lines"] if cur else intro_lines).append(raw)
    closing = None
    if blocks:
        # Trailing farewell (blank-line separated) of the LAST block → closing.
        body = "\n".join(blocks[-1]["lines"]).strip()
        parts = re.split(r"\n\s*\n", body)
        if len(parts) > 1 and len(parts[-1]) < 260:
            closing = parts[-1].strip()
            blocks[-1]["lines"] = "\n\n".join(parts[:-1]).split("\n")
    out = []
    for b in blocks:
        body = "\n".join(b["lines"]).strip()
        if body:
            out.append({"title": b["title"], "html": _inline(body)})
    intro = "\n".join(intro_lines).strip()
    return (_inline(intro) if intro else None, out,
            _inline(closing) if closing else None)


# Concept → color + icon (Phosphor-style inline SVG, white stroke). Matched by
# keyword against the LLM block titles.
_CONCEPTS = [
    (("pagas", "total"), "#2c5171",
     '<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.4"><rect x="2" y="5" width="20" height="14" rx="2"/><line x1="2" y1="10" x2="22" y2="10"/></svg>'),
    (("energía",), "#4682b4",
     '<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.4"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>'),
    (("potencia",), "#8e6cc8",
     '<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.4"><path d="M12 20V10"/><path d="M18 20V4"/><path d="M6 20v-4"/></svg>'),
    (("peajes", "cargos"), "#f39c12",
     '<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.4"><path d="M4 21V8l8-5 8 5v13"/><path d="M9 21v-6h6v6"/></svg>'),
    (("bono",), "#2ecc71",
     '<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.4"><path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1-1.1a5.5 5.5 0 0 0-7.8 7.8l8.8 8.9 8.8-8.9a5.5 5.5 0 0 0 0-7.8z"/></svg>'),
    (("impuestos",), "#e74c3c",
     '<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.4"><line x1="19" y1="5" x2="5" y2="19"/><circle cx="6.5" cy="6.5" r="2.5"/><circle cx="17.5" cy="17.5" r="2.5"/></svg>'),
    (("comparada", "anterior"), "#16a085",
     '<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.4"><polyline points="23 6 13.5 15.5 8.5 10.5 1 18"/><polyline points="17 6 23 6 23 12"/></svg>'),
]
_DEFAULT_CONCEPT = ("#4682b4",
    '<svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.4"><circle cx="12" cy="12" r="9"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12" y2="16"/></svg>')


def _tint(hexcolor: str) -> str:
    """Very light background from a hex color (WeasyPrint-safe rgba)."""
    h = hexcolor.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},0.09)"


def _decorate_blocks(blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for b in blocks:
        tl = b["title"].lower()
        color, icon = _DEFAULT_CONCEPT
        for keys, c, ic in _CONCEPTS:
            if any(k in tl for k in keys):
                color, icon = c, ic
                break
        b["color"], b["icon"], b["bg"] = color, icon, _tint(color)
    return blocks


_MONEY = [("energia_eur", "Energía", "#4682b4"),
          ("potencia_eur", "Potencia", "#8e6cc8"),
          ("peajes_eur", "Peajes y cargos", "#f39c12"),
          ("bono_social_eur", "Bono social", "#2ecc71"),
          ("alquiler_eur", "Alquiler contador", "#6a9bc3"),
          ("impuestos_eur", "Impuestos", "#e74c3c")]


def _money_parts(amounts: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not amounts:
        return []
    vals = [(label, color, float(amounts[k]))
            for k, label, color in _MONEY
            if amounts.get(k) not in (None, 0)]
    total = sum(v for _, _, v in vals)
    if total <= 0 or len(vals) < 2:
        return []
    return [{"label": lbl, "color": col, "amount": _eur(v),
             "pct": max(round(v / total * 100, 1), 1.5)}
            for lbl, col, v in vals]


def _band_cards(amounts: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not amounts:
        return []
    spec = [("kwh_horas_caras", "precio_horas_caras", "Horas caras", "p1"),
            ("kwh_horas_normales", "precio_horas_normales", "Horas normales", "p2"),
            ("kwh_horas_baratas", "precio_horas_baratas", "Horas baratas", "p3")]
    out = []
    for kk, pk, label, cls in spec:
        if amounts.get(kk):
            out.append({"label": label, "cls": cls,
                        "kwh": f"{float(amounts[kk]):.0f}",
                        "price": (f"{float(amounts[pk]):.4f}".replace(".", ",")
                                  if amounts.get(pk) else None)})
    return out if len(out) >= 2 else []


def render(inv: Dict[str, Any], explanation: str,
           anomaly: Optional[Dict[str, Any]] = None,
           amounts: Optional[Dict[str, Any]] = None) -> bytes:
    from weasyprint import HTML

    intro, blocks, closing = _parse_blocks(explanation or "")
    blocks = _decorate_blocks(blocks)

    days = None
    try:
        days = (date.fromisoformat(inv["period_end"])
                - date.fromisoformat(inv["period_start"])).days + 1
    except (TypeError, ValueError):
        pass
    eur_day = eur_day_ref = None
    sig = (anomaly or {}).get("signals") or {}
    if sig.get("gasto_por_dia_esta_factura_eur") is not None:
        eur_day = _eur(sig["gasto_por_dia_esta_factura_eur"]) + "/día"
        if sig.get("gasto_por_dia_habitual_eur") is not None:
            eur_day_ref = "habitual " + _eur(sig["gasto_por_dia_habitual_eur"])
    elif inv.get("total_eur") and days:
        eur_day = _eur(inv["total_eur"] / days) + "/día"

    bd = inv.get("breakdown") or {}
    ex = bd.get("extracted") or {}
    retailer = ex.get("retailer")
    if not retailer:
        for s in bd.get("segments") or []:
            retailer = ((s.get("contract") or {}).get("retailer"))
            if retailer:
                break

    narrative = (anomaly or {}).get("narrative") or {}
    anom_ctx = None
    dev_pct = sig.get("desviacion_gasto_pct")
    if anomaly and anomaly.get("status") == "ok" and narrative.get("headline"):
        anom_ctx = {"level": anomaly.get("level") or "normal",
                    "headline": narrative.get("headline"),
                    "causes": narrative.get("causes") or [],
                    "pct": (f"{'+' if dev_pct > 0 else ''}{dev_pct:.0f} %"
                            if dev_pct is not None else "±")}
    trend = "flat"
    if dev_pct is not None:
        trend = "up" if dev_pct >= 10 else "down" if dev_pct <= -10 else "flat"

    logo_svg = ""
    try:
        with open(_LOGO, encoding="utf-8") as fh:
            logo_svg = fh.read()
    except OSError:
        pass

    # An untitled intro becomes a first "Resumen" block so nothing is lost.
    if intro:
        blocks.insert(0, {"title": "En resumen", "html": intro,
                          "color": "#2c5171", "bg": _tint("#2c5171"),
                          "icon": _CONCEPTS[0][2]})

    energy_kwh = None
    if inv.get("energy_kwh"):
        energy_kwh = f"{inv['energy_kwh']:.0f}"
    elif amounts:
        tot_kwh = sum(float(amounts.get(k) or 0) for k in
                      ("kwh_horas_caras", "kwh_horas_normales", "kwh_horas_baratas"))
        if tot_kwh:
            energy_kwh = f"{tot_kwh:.0f}"

    html = _env.get_template("explain_pdf.html").render(
        fonts_dir=_FONTS,
        logo_svg=logo_svg,
        retailer=retailer,
        total=_eur(inv.get("total_eur")),
        period=f"{inv.get('period_start')} → {inv.get('period_end')}",
        days=days or "—",
        eur_day=eur_day, eur_day_ref=eur_day_ref, trend=trend,
        energy_kwh=energy_kwh,
        anomaly=anom_ctx,
        money_parts=_money_parts(amounts),
        bands=_band_cards(amounts),
        blocks=blocks, closing=closing,
        generated=datetime.now().strftime("%d/%m/%Y %H:%M"),
    )
    return HTML(string=html, base_url=_BASE).write_pdf()
