"""Email transport (S1) — api_consum -> api_auth centralized sender.

api_consum owns NO SMTP config of its own: it POSTs to api_auth's
`/api/v1/email/send`, which resolves SMTP (service-specific -> global
fallback) and renders the branded template server-side. Mirrors api_edge's
`notification_dispatch_service._send_via_api_auth_template` payload shape
exactly: {template_type, to_email, lang, context, service}.

Dark launch: `EMAIL_ADVICE_ENABLED` (default False) short-circuits BEFORE any
network call — flip it on once the advice email has been reviewed.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from app.core import http_client
from app.core.config import settings

logger = logging.getLogger("consum.mailer")

_SEND_TIMEOUT_S = 15.0


async def send_email(template_type: str, to_email: str, context: Dict[str, Any],
                     lang: str = "es") -> bool:
    """POST {AUTH_BASE_URL}/api/v1/email/send. Returns True on success.

    Dark-launch gate: when EMAIL_ADVICE_ENABLED is False this returns False
    immediately WITHOUT calling api_auth — callers (advice_scheduler) use this
    to log "would have sent" during rollout without touching a mailbox.
    """
    if not settings.EMAIL_ADVICE_ENABLED:
        logger.info("EMAIL_ADVICE_ENABLED=false — skipping %s email to %s (dark launch)",
                    template_type, to_email)
        return False
    if not to_email:
        logger.warning("send_email(%s): no recipient — skipping", template_type)
        return False

    base = (settings.AUTH_BASE_URL or "").rstrip("/")
    if not base or not settings.AUTH_SERVICE_API_KEY:
        logger.error("send_email(%s): AUTH_BASE_URL / AUTH_SERVICE_API_KEY not configured", template_type)
        return False

    try:
        client = http_client.get_client()
        resp = await client.post(
            f"{base}/api/v1/email/send",
            headers={"X-API-Key": settings.AUTH_SERVICE_API_KEY},
            json={"template_type": template_type, "to_email": to_email,
                  "lang": lang, "context": context, "service": "api_consum"},
            timeout=_SEND_TIMEOUT_S,
        )
        if resp.status_code != 200:
            logger.error("api_auth /email/send -> %s: %s", resp.status_code, resp.text[:300])
            return False
        logger.info("sent %s email to %s (lang=%s)", template_type, to_email, lang)
        return True
    except Exception as exc:
        logger.error("send_email(%s) to %s failed: %s", template_type, to_email, exc)
        return False
