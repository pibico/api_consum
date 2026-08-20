"""Raw text layer of a PDF — the safety net under api_convert (docling).

docling's markdown is a LAYOUT reconstruction: it rebuilds tables beautifully
but silently drops runs of text it classifies as furniture. Seen live on a
Masnorte bill (invoice 69): the PDF's own text layer contains
`Datos referidos al CUPS: ES0026000000796350SZ`, yet that line never reached
the markdown — so the CUPS came back null, the bill was stored unattached to
its supply point, and `GET /invoices?supply=<id>` filtered it out of the list
entirely. From the user's side the import had vanished.

This module reads the PDF's embedded text directly, with no layout smarts. It
is NOT a replacement for the conversion (no tables, no OCR — a scanned photo
has no text layer and yields ""); it exists only so the DETERMINISTIC regex
fallbacks in `services/invoices.store_uploaded` can see everything the
document actually says, not just what docling chose to keep.

Never raises: a missing pypdf, an encrypted or malformed file all degrade to
None and the caller falls back to the markdown alone.
"""
from __future__ import annotations

import io
import logging
from typing import Optional

logger = logging.getLogger("consum.pdftext")

_MAX_PAGES = 12          # a domestic bill is 2-6 pages; cap runaway documents


def extract(pdf_bytes: bytes, max_pages: int = _MAX_PAGES) -> Optional[str]:
    """The concatenated text layer of `pdf_bytes`, or None when unavailable
    (not a PDF, encrypted, scanned image with no text, pypdf missing)."""
    if not pdf_bytes or not pdf_bytes[:5].startswith(b"%PDF"):
        return None
    try:
        from pypdf import PdfReader
    except ImportError:                                      # noqa: BLE001
        logger.warning("pypdf not installed — raw PDF text fallback disabled")
        return None
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        parts = [(page.extract_text() or "") for page in reader.pages[:max_pages]]
    except Exception as exc:                                 # noqa: BLE001
        logger.info("raw PDF text extraction failed: %s", exc)
        return None
    text = "\n".join(p for p in parts if p.strip())
    return text or None
