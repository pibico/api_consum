"""Shared async HTTP client for outbound calls to api_auth.

Vendored from api_edge (core/http_client.py, 2.1a pattern): before this,
`endpoints/auth.py`'s OTP/validate proxies each opened a brand-new
`httpx.AsyncClient()` per call — a fresh TCP+TLS handshake on every single
request instead of a pooled, reused connection. One module-level client fixes
that; call `aclose()` from the app lifespan shutdown to release connections
cleanly (best-effort only — the process exiting reclaims sockets regardless,
so this isn't a correctness requirement, just avoids httpx "unclosed client"
warnings in logs).
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger("api_consum.http_client")

_client: Optional[httpx.AsyncClient] = None

# Sane default for internal service-to-service calls (api_auth lives on the
# same box/VPN in every deployment). Individual call sites may still pass a
# per-request `timeout=` override for slower operations.
_DEFAULT_TIMEOUT = httpx.Timeout(5.0, connect=5.0)


def get_client() -> httpx.AsyncClient:
  """Return the shared client, creating it lazily on first use.

  Lazy + idempotent: safe to call from any coroutine, recreates the client
  transparently if a previous one was closed (e.g. between test cases).
  """
  global _client
  if _client is None or _client.is_closed:
    _client = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT)
    logger.debug("shared httpx.AsyncClient created")
  return _client


async def aclose() -> None:
  """Close the shared client. Safe to call multiple times / before creation."""
  global _client
  if _client is not None and not _client.is_closed:
    await _client.aclose()
    logger.debug("shared httpx.AsyncClient closed")
  _client = None
