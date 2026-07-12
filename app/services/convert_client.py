"""api_convert (api.pibico.es/convert) client — document → Markdown.

Server-side only: the browser never talks to api_convert directly and the key
never leaves this process. Turns an uploaded PDF/image/doc into Markdown text so
the AI skills (`services/ai/`) can extract structured data from it — tariff
sheets and, later, the user's contract document (and, in the invoice phase,
bills). SHARED ingestion utility, reused across domains.

Contract (verified against the live OpenAPI at
`api.pibico.es/convert/api/v1/openapi.json` — "Document Conversion API", docling):
    POST {CONVERT_BASE_URL}/api/v1/convert
    headers: X-API-Key: <CONVERT_API_KEY>   (optional server-side; sent for tracking)
    multipart form: file (required), + flags: output_format(markdown|json|html|
        doctags), detect_tables, use_vlm (OCR for scans/photos), extract_images,
        page_range …
    -> ConversionResponse {success, filename, markdown, metadata, pages, tables,
        images, processing_time, vlm_used, error}

`detect_tables=True` by default — tariff sheets are price TABLES (P1/P2/P3), and
without it the markdown loses the numbers. `use_vlm` on for scanned/photo inputs.

Conversion is OFF until CONVERT_API_KEY is set in .env (per-tenant key model,
like CHAT_API_KEY). Returns None on any failure — callers degrade gracefully;
the page never breaks because a document couldn't be converted.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from app.core import http_client
from app.core.config import settings

logger = logging.getLogger("consum.convert")


def configured() -> bool:
    return bool(settings.CONVERT_API_KEY)


def _extract_markdown(data: Any) -> Optional[str]:
    """Defensive parse — the hub returns JSON but the markdown field name isn't
    pinned by a typed spec (pibico_hub falls back to `{'markdown': text}`)."""
    if isinstance(data, str):
        return data.strip() or None
    if not isinstance(data, dict):
        return None
    for key in ("markdown", "md", "content", "text", "result"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v
    inner = data.get("data")           # nested {data: {markdown: ...}}
    if isinstance(inner, dict):
        return _extract_markdown(inner)
    return None


async def to_markdown(file_bytes: bytes, filename: str,
                      content_type: str = "application/octet-stream",
                      detect_tables: bool = True, use_vlm: bool = False,
                      timeout: float = 180.0) -> Optional[str]:
    """POST a document to api_convert and return its Markdown text. None if not
    configured or on any failure (network / non-200 / success:false / no markdown).

    detect_tables: keep the price tables (P1/P2/P3) — on by default.
    use_vlm: run the vision OCR path — enable for scanned PDFs / photos."""
    if not configured():
        logger.info("api_convert not configured (CONVERT_API_KEY empty)")
        return None
    url = f"{settings.CONVERT_BASE_URL.rstrip('/')}/api/v1/convert"
    files = {"file": (filename, file_bytes, content_type or "application/octet-stream")}
    data = {
        "output_format": "markdown",
        "detect_tables": str(detect_tables).lower(),
        "use_vlm": str(use_vlm).lower(),
    }
    try:
        client = http_client.get_client()
        r = await client.post(url, headers={"X-API-Key": settings.CONVERT_API_KEY},
                              files=files, data=data, timeout=timeout)
    except httpx.RequestError as e:
        logger.error("api_convert unreachable: %s", e)
        return None
    if r.status_code != 200:
        logger.warning("api_convert %s -> %s: %s", filename, r.status_code, r.text[:200])
        return None
    try:
        payload = r.json()
    except ValueError:                 # body wasn't JSON — treat as raw markdown
        return (r.text or "").strip() or None
    if isinstance(payload, dict) and payload.get("success") is False:
        logger.warning("api_convert %s success=false: %s", filename, payload.get("error"))
        return None
    md = _extract_markdown(payload)
    if not md:
        logger.warning("api_convert response had no markdown: %s", str(r.text)[:200])
    return md
