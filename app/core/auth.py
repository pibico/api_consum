"""api_consum auth — vendored remote-only auth_client (api_edge/api_exo pattern).

api_consum holds no local JWT secret (ADR-9) — every credential resolves via
the cached api_auth `/validate`. Only `ADMIN_API_KEY` is a local superadmin
escape hatch; the regular `API_KEY` must NOT grant admin (auth_client maps
any key in LOCAL_API_KEYS to a synthetic SUPERADMIN, so it is deliberately
excluded there and accepted in `require_auth_always` as a synthetic NON-admin
service user — mirrors api_edge core/auth.py exactly).

Also hosts `_validate_jwt` + short-TTL positive/negative caches (api_edge
2.1a/b), used by the web page guard (endpoints/web.py) and the org/tier
resolver (dependencies/rbac.py).
"""
import hashlib
import logging
import secrets as _secrets
import time
from typing import Optional

import httpx
from fastapi import Depends, HTTPException, Request, status

from app.core import http_client
from app.core.config import settings
from app.utils import auth_client
from app.utils.auth_client import AuthUser

logger = logging.getLogger("consum.auth")

AUTH_VALIDATE_URL = f"{settings.AUTH_BASE_URL.rstrip('/')}/api/v1/auth/validate"

# Configure the client remote-only: empty jwt_secret → no local HS256 decode,
# always cached /validate. Only ADMIN_API_KEY as local superadmin escape.
auth_client.configure(
    auth_url=settings.AUTH_BASE_URL,
    local_api_keys=settings.ADMIN_API_KEY or "",
    jwt_secret="",
)

# Per-service admin gate (api_consum registered in api_auth service_registry).
require_admin = auth_client.require_service_role("api_consum", "admin")

# Authenticated resolver (JWT via /validate OR service X-API-Key). 401 if none.
verify_auth = auth_client.get_current_user


# ---------------------------------------------------------------------------
# Short-TTL validate cache — same as api_edge/api_exo core/auth.py.
# ---------------------------------------------------------------------------
_validate_cache: dict[str, tuple[float, dict]] = {}
_validate_negative_cache: dict[str, float] = {}


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:32]


def _cache_get(token_hash: str) -> Optional[dict]:
    entry = _validate_cache.get(token_hash)
    if entry and time.time() < entry[0]:
        return entry[1]
    return None


def _cache_set(token_hash: str, payload: dict) -> None:
    _validate_cache[token_hash] = (time.time() + settings.AUTH_VALIDATE_CACHE_TTL, payload)
    _validate_negative_cache.pop(token_hash, None)


def _negative_cached(token_hash: str) -> bool:
    expires_at = _validate_negative_cache.get(token_hash)
    return expires_at is not None and time.time() < expires_at


def _negative_cache_set(token_hash: str) -> None:
    _validate_negative_cache[token_hash] = time.time() + settings.AUTH_VALIDATE_NEGATIVE_CACHE_TTL


def clear_validate_cache() -> None:
    """Test hook — drop both caches."""
    _validate_cache.clear()
    _validate_negative_cache.clear()


async def _validate_jwt(token: str) -> dict:
    """Validate a JWT against api_auth — pooled client + short-TTL caches.
    Raises 401/503. Returns the full /validate payload (orgs with customers,
    service_roles incl. plan/ai_enabled — everything rbac.py needs)."""
    th = _token_hash(token)

    cached = _cache_get(th)
    if cached is not None:
        return cached

    if _negative_cached(th):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    try:
        client = http_client.get_client()
        response = await client.get(
            AUTH_VALIDATE_URL,
            headers={"Authorization": f"Bearer {token}"},
        )

        if response.status_code == 200:
            data = response.json()
            _cache_set(th, data)
            return data

        logger.warning("JWT validation failed: %s", response.status_code)
        _negative_cache_set(th)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")

    except HTTPException:
        raise
    except httpx.RequestError as e:
        logger.error("api_auth unreachable: %s", e)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication service unavailable",
        )


def _service_key_user(request: Request) -> Optional[AuthUser]:
    """Accept the regular service `API_KEY` (X-API-Key) as a synthetic
    NON-admin user. Never added to auth_client LOCAL_API_KEYS (that path
    would grant superadmin)."""
    key = request.headers.get("x-api-key") or ""
    if key and settings.API_KEY and _secrets.compare_digest(key, settings.API_KEY):
        return AuthUser(id=0, email="api-key-user", name="API Key", is_superadmin=False)
    return None


def require_auth_always(request: Request, user=Depends(auth_client.get_optional_user)):
    """Hard auth gate: JWT (Bearer/cookie), ADMIN_API_KEY (via auth_client) or
    the regular service API_KEY (synthetic non-admin)."""
    if user is None:
        user = _service_key_user(request)
    if user is None:
        raise HTTPException(401, "Authentication required")
    return user
