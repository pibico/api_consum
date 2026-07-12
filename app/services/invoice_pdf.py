"""Branded invoice PDF rendering (WeasyPrint).

render(invoice) → PDF bytes. Synchronous & CPU-bound (~1-2 s) — the endpoint
calls it via asyncio.to_thread. The invoice dict is the frozen document from
services/invoices (period, totals, breakdown.segments with contract snapshots).

Fonts: brand TTFs live in static/fonts/pdf/ (WOFF2→TTF, see the fonts dir);
WeasyPrint embeds them via @font-face with absolute file paths, falling back
to the system sans if absent. The pibiCo logo SVG is inlined.
"""
from __future__ import annotations

import os
from datetime import date
from typing import Any, Dict

from jinja2 import Environment, FileSystemLoader, select_autoescape

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


def _kwh(v: Any) -> str:
    try:
        return f"{float(v):,.2f}".replace(",", "·").replace(".", ",").replace("·", ".")
    except (TypeError, ValueError):
        return "—"


_env.filters["eur"] = _eur
_env.filters["kwh"] = _kwh

_TYPE_LABEL = {"fixed": "Precio fijo", "indexed": "Indexado", "pvpc": "PVPC (regulado)"}


def render(invoice: Dict[str, Any]) -> bytes:
    from weasyprint import HTML

    try:
        with open(_LOGO, encoding="utf-8") as fh:
            logo_svg = fh.read()
    except OSError:
        logo_svg = ""

    breakdown = invoice.get("breakdown") or {}
    segments = breakdown.get("segments") or []
    for seg in segments:
        c = seg.get("contract") or {}
        seg["_type_label"] = _TYPE_LABEL.get(seg.get("contract_type"), seg.get("contract_type"))
        seg["_retailer"] = c.get("retailer")
        seg["_label"] = c.get("label")

    html = _env.get_template("invoice_pdf.html").render(
        inv=invoice,
        segments=segments,
        totals=breakdown.get("totals") or {},
        logo_svg=logo_svg,
        fonts_dir=_FONTS,
        type_label=_TYPE_LABEL,
        generated=date.today().isoformat(),
    )
    # base_url = templates dir so any relative refs resolve; fonts use absolute
    # paths in the @font-face block, so this is belt-and-braces.
    return HTML(string=html, base_url=_TEMPLATES).write_pdf()
