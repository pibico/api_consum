#!/usr/bin/env python3
"""
auth_client.py — Drop-in auth dependency for pibiCo microservices.

Copy this file into each service's app/utils/ directory.
Add to .env:
    JWT_AUTH_SECRET=<shared-secret-from-api_auth>
    AUTH_SERVICE_URL=https://api.pibico.es/auth

Two verification paths:
  Fast path: local HS256 JWT decode (no network call)
  Slow path: HTTP GET to api_auth /api/v1/auth/validate (cached 5 min)

Backward compatible: supports X-API-Key (legacy), Bearer JWT, httpOnly cookie.
"""
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Tuple

import httpx
from fastapi import Cookie, Depends, HTTPException, Query, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — override these after import, or set via .env
# ---------------------------------------------------------------------------

# Shared HS256 secret (same as api_auth's JWT_SECRET)
JWT_AUTH_SECRET: str = ""
JWT_ALGORITHM: str = "HS256"

# URL of the central auth service
AUTH_SERVICE_URL: str = "https://api.pibico.es/auth"

# Legacy API keys from this service's .env (comma-separated)
LOCAL_API_KEYS: list[str] = []

# httpOnly cookie name (default: auth_jwt from api_auth)
COOKIE_NAME: str = "auth_jwt"


def configure(
    jwt_secret: str = "",
    auth_url: str = "",
    local_api_keys: str = "",
    cookie_name: str = "auth_jwt",
):
    """Call once at app startup to configure the auth client."""
    global JWT_AUTH_SECRET, AUTH_SERVICE_URL, LOCAL_API_KEYS, COOKIE_NAME
    if jwt_secret:
        JWT_AUTH_SECRET = jwt_secret
    if auth_url:
        AUTH_SERVICE_URL = auth_url.rstrip("/")
    if local_api_keys:
        LOCAL_API_KEYS = [k.strip() for k in local_api_keys.split(",") if k.strip()]
    if cookie_name:
        COOKIE_NAME = cookie_name


# ---------------------------------------------------------------------------
# AuthUser — unified user object returned by all dependencies
# ---------------------------------------------------------------------------

@dataclass
class AuthUser:
    """Unified user object. Compatible with existing User model access patterns."""
    id: int = 0
    email: str = ""
    name: str = ""
    is_active: bool = True
    is_superadmin: bool = False
    avatar_url: Optional[str] = None
    credits: int = 0
    tier: str = "free"
    totp_verified: bool = False
    orgs: list[dict] = field(default_factory=list)
    # Set by get_org_context
    _org_role: Optional[str] = field(default=None, repr=False)


# Backward compat: AuthContext for api_chat
@dataclass
class AuthContext:
    """Backward-compatible with api_chat's AuthContext."""
    api_key_id: Optional[str] = None
    is_admin: bool = False


# ---------------------------------------------------------------------------
# Cache for slow-path validation (same pattern as api_chat)
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, Optional[dict]]] = {}
_CACHE_TTL = 300  # 5 minutes
_CACHE_MAX = 1000


def _cache_get(key: str) -> Optional[dict]:
    entry = _cache.get(key)
    if entry and (time.time() - entry[0]) < _CACHE_TTL:
        return entry[1]
    return None


def _cache_set(key: str, value: Optional[dict]):
    _cache[key] = (time.time(), value)
    if len(_cache) > _CACHE_MAX:
        cutoff = time.time() - _CACHE_TTL
        expired = [k for k, (t, _) in _cache.items() if t < cutoff]
        for k in expired:
            _cache.pop(k, None)


# ---------------------------------------------------------------------------
# Fast path: local JWT decode
# ---------------------------------------------------------------------------

def _decode_jwt_local(token: str) -> Optional[dict]:
    """Decode HS256 JWT locally. Returns payload or None."""
    if not JWT_AUTH_SECRET:
        return None
    try:
        payload = jwt.decode(token, JWT_AUTH_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("scope") not in ("authenticated", None):
            return None
        return payload
    except JWTError:
        return None


def _decode_pre2fa_local(token: str) -> Optional[dict]:
    """Decode pre_2fa scoped JWT locally."""
    if not JWT_AUTH_SECRET:
        return None
    try:
        payload = jwt.decode(token, JWT_AUTH_SECRET, algorithms=[JWT_ALGORITHM])
        if payload.get("scope") != "pre_2fa":
            return None
        return payload
    except JWTError:
        return None


# ---------------------------------------------------------------------------
# Slow path: HTTP validation to api_auth
# ---------------------------------------------------------------------------

def _validate_remote(token: str = "", api_key: str = "") -> Optional[dict]:
    """Call api_auth /validate endpoint. Synchronous with caching."""
    cache_key = f"t:{hashlib.sha256(token.encode()).hexdigest()[:32]}" if token else f"k:{hashlib.sha256(api_key.encode()).hexdigest()[:32]}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif api_key:
        headers["X-API-Key"] = api_key

    try:
        resp = httpx.get(
            f"{AUTH_SERVICE_URL}/api/v1/auth/validate",
            headers=headers,
            timeout=5.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            _cache_set(cache_key, data)
            return data
        _cache_set(cache_key, None)
        return None
    except Exception as e:
        logger.warning("auth_client: remote validation failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Internal: resolve token/key to AuthUser
# ---------------------------------------------------------------------------

def _resolve(token: str = "", api_key: str = "") -> Optional[AuthUser]:
    """Resolve a token or API key to an AuthUser."""
    # 1. Legacy local API key check
    if api_key and api_key in LOCAL_API_KEYS:
        # Validate against api_auth to get user info, or return synthetic superadmin
        data = _validate_remote(api_key=api_key)
        if data:
            return _dict_to_user(data)
        # Fallback: synthetic superadmin for backward compat
        return AuthUser(id=0, email="api-key@local", is_superadmin=True, credits=999999)

    # 2. JWT fast path (local decode)
    if token:
        payload = _decode_jwt_local(token)
        if payload:
            user = AuthUser(
                id=int(payload.get("sub", 0)),
                email=payload.get("email", ""),
                is_superadmin=False,  # enriched via slow path if needed
            )
            # Try slow path for full user data (cached)
            full = _validate_remote(token=token)
            if full:
                return _dict_to_user(full)
            return user

    # 3. API key via remote validation
    if api_key:
        data = _validate_remote(api_key=api_key)
        if data:
            return _dict_to_user(data)

    # 4. Token via remote only (if local decode failed — e.g. different secret)
    if token:
        data = _validate_remote(token=token)
        if data:
            return _dict_to_user(data)

    return None


def _dict_to_user(d: dict) -> AuthUser:
    return AuthUser(
        id=d.get("user_id", d.get("id", 0)),
        email=d.get("email", ""),
        name=d.get("name", ""),
        is_active=d.get("is_active", True),
        is_superadmin=d.get("is_superadmin", False),
        avatar_url=d.get("avatar_url"),
        credits=d.get("credits", 0),
        tier=d.get("tier", "free"),
        orgs=d.get("orgs", []),
    )


# ---------------------------------------------------------------------------
# FastAPI security extractors
# ---------------------------------------------------------------------------

_bearer = HTTPBearer(auto_error=False)
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_org_id_header = APIKeyHeader(name="X-Org-Id", auto_error=False)


# ---------------------------------------------------------------------------
# Public dependencies — drop-in replacements
# ---------------------------------------------------------------------------

def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
    api_key: Optional[str] = Security(_api_key_header),
    api_key_query: Optional[str] = Query(None, alias="api_key"),
    auth_jwt: Optional[str] = Cookie(None),
) -> AuthUser:
    """
    Primary auth dependency. Accepts:
    - Authorization: Bearer <jwt>
    - X-API-Key header or ?api_key= query
    - httpOnly cookie (auth_jwt)
    Raises 401 if none valid.
    """
    token = ""
    key = api_key or api_key_query or ""

    if credentials:
        token = credentials.credentials
    elif auth_jwt:
        token = auth_jwt

    user = _resolve(token=token, api_key=key)
    if not user:
        raise HTTPException(401, "Authentication required")
    if not user.is_active:
        raise HTTPException(403, "User account is inactive")
    return user


def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
    api_key: Optional[str] = Security(_api_key_header),
    auth_jwt: Optional[str] = Cookie(None),
) -> Optional[AuthUser]:
    """Same as get_current_user but returns None instead of raising."""
    token = ""
    key = api_key or ""

    if credentials:
        token = credentials.credentials
    elif auth_jwt:
        token = auth_jwt

    if not token and not key:
        return None

    user = _resolve(token=token, api_key=key)
    if user and user.is_active:
        return user
    return None


def get_pre2fa_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
) -> AuthUser:
    """Validate pre_2fa scoped JWT. Bearer only (no cookie)."""
    if not credentials:
        raise HTTPException(401, "Pre-2FA token required")
    payload = _decode_pre2fa_local(credentials.credentials)
    if not payload:
        raise HTTPException(401, "Invalid or expired pre-2FA token")
    return AuthUser(id=int(payload.get("sub", 0)), email=payload.get("email", ""))


def get_org_context(
    user: AuthUser = Depends(get_current_user),
    org_id_header: Optional[str] = Security(_org_id_header),
) -> Tuple[AuthUser, Optional[int]]:
    """
    Extract org context from X-Org-Id header.
    Superadmin: any org or None. Regular user: must be member.
    """
    if user.is_superadmin:
        org_id = int(org_id_header) if org_id_header else None
        return user, org_id

    if not user.orgs:
        raise HTTPException(403, "User has no organization memberships")

    if org_id_header:
        requested = int(org_id_header)
        member = next((o for o in user.orgs if o.get("id") == requested), None)
        if not member:
            raise HTTPException(403, "Not a member of this organization")
        user._org_role = member.get("role")
        return user, requested

    # Default to first org
    user._org_role = user.orgs[0].get("role")
    return user, user.orgs[0].get("id")


# Role hierarchy (must match api_auth's ROLE_RANK)
ROLE_RANK = {
    "viewer": 0, "member": 1, "editor": 2, "admin": 3, "owner": 4,
}


def require_role(min_role: str):
    """Dependency factory for ORG-LEVEL role-based access control.

    Checks the caller's org-level role (orgs[].role via X-Org-Id). For per-service
    granularity use require_service_role().
    """
    def _dep(ctx: Tuple[AuthUser, Optional[int]] = Depends(get_org_context)):
        user, org_id = ctx
        if user.is_superadmin:
            return ctx
        if org_id is None:
            raise HTTPException(403, "Organization context required")
        role = user._org_role
        if role is None:
            raise HTTPException(403, "Not a member of this organization")
        if ROLE_RANK.get(role, 0) < ROLE_RANK.get(min_role, 0):
            raise HTTPException(403, f"Requires {min_role} role or higher")
        return ctx
    return _dep


def require_service_role(service: str, min_role: str):
    """Dependency factory for PER-SERVICE role-based access control.

    Unlike require_role (org-level), this enforces the caller's role for a specific
    service via orgs[].service_roles[service].role. When the user has no explicit
    service membership (role is None) it falls back to the org-level role. Superadmin
    bypasses. Requires org context (X-Org-Id header, or the user's single org).

    The resolved org MUST have access to `service` (i.e. `service` appears in its
    service_roles map, which api_auth builds from service_access); otherwise 403.

    Note: service_roles is only populated on the slow /validate path — get_org_context
    (→ get_current_user → _resolve) triggers it, so this dependency always has it when
    api_auth is reachable.
    """
    def _dep(ctx: Tuple[AuthUser, Optional[int]] = Depends(get_org_context)):
        user, org_id = ctx
        if user.is_superadmin:
            return ctx
        if org_id is None:
            raise HTTPException(403, "Organization context required")
        org = next((o for o in user.orgs if o.get("id") == org_id), None)
        if org is None:
            raise HTTPException(403, "Not a member of this organization")
        service_roles = org.get("service_roles") or {}
        if service not in service_roles:
            raise HTTPException(403, f"Organization has no access to {service}")
        # Per-service role; None = no explicit service membership → org-level role
        role = (service_roles[service] or {}).get("role") or org.get("role")
        if role is None:
            raise HTTPException(403, "No role for this organization")
        if ROLE_RANK.get(role, 0) < ROLE_RANK.get(min_role, 0):
            raise HTTPException(403, f"Requires {min_role} role or higher on {service}")
        return ctx
    return _dep


def require_superadmin(
    user: AuthUser = Depends(get_current_user),
) -> AuthUser:
    """Requires superadmin. Raises 403 otherwise."""
    if not user.is_superadmin:
        raise HTTPException(403, "Superadmin required")
    return user


def require_credits(operation: str, cost: int):
    """
    Dependency factory for credit-gated operations.
    Calls api_auth POST /credits/deduct to deduct centrally.
    Superadmin/API-key: skip deduction.
    """
    def _dep(
        user: Optional[AuthUser] = Depends(get_optional_user),
    ) -> Optional[AuthUser]:
        if not user:
            return None  # API-key-only request
        if user.is_superadmin:
            return user
        if user.credits < cost:
            raise HTTPException(402, f"Insufficient credits: need {cost}, have {user.credits}")
        # Deduct via api_auth
        try:
            resp = httpx.post(
                f"{AUTH_SERVICE_URL}/api/v1/credits/deduct",
                json={"amount": cost, "operation": operation, "service": "unknown"},
                headers={"Authorization": f"Bearer {JWT_AUTH_SECRET}"},
                timeout=5.0,
            )
            if resp.status_code == 200:
                user.credits = resp.json().get("credits_remaining", user.credits - cost)
            else:
                logger.warning("Credit deduction failed: %s", resp.text)
                user.credits -= cost  # optimistic local deduct
        except Exception as e:
            logger.warning("Credit deduction HTTP failed: %s", e)
            user.credits -= cost
        return user
    return _dep


# ---------------------------------------------------------------------------
# Legacy backward-compatible functions
# ---------------------------------------------------------------------------

def verify_api_key(
    api_key: Optional[str] = Security(_api_key_header),
    api_key_query: Optional[str] = Query(None, alias="api_key"),
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
) -> str:
    """
    Legacy gate: validate X-API-Key OR Bearer JWT.
    Returns the validated credential string.
    For services that just need a simple auth gate (api_tg pattern).
    """
    key = api_key or api_key_query

    # API key path
    if key:
        if key in LOCAL_API_KEYS:
            return key
        data = _validate_remote(api_key=key)
        if data:
            return key

    # Bearer JWT path
    if credentials:
        token = credentials.credentials
        payload = _decode_jwt_local(token)
        if payload:
            return token
        data = _validate_remote(token=token)
        if data:
            return token

    if not key and not credentials:
        raise HTTPException(401, "API key or Bearer token required")
    raise HTTPException(403, "Invalid credentials")


def get_api_key(
    api_key_header_value: Optional[str] = Security(_api_key_header),
    api_key_query: Optional[str] = Query(None, alias="api_key"),
) -> str:
    """Alias for verify_api_key (api_chat/api_cad compat)."""
    return verify_api_key(api_key_header_value, api_key_query)


def get_auth_context(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
    api_key: Optional[str] = Security(_api_key_header),
    api_key_query: Optional[str] = Query(None, alias="api_key"),
    auth_jwt: Optional[str] = Cookie(None),
) -> AuthContext:
    """
    api_chat backward compat: returns AuthContext with api_key_id and is_admin.
    Master keys → is_admin=True. JWT → is_admin based on is_superadmin.
    """
    key = api_key or api_key_query or ""
    token = ""

    if credentials:
        token = credentials.credentials
    elif auth_jwt:
        token = auth_jwt

    # Local master key → admin
    if key and key in LOCAL_API_KEYS:
        return AuthContext(api_key_id=None, is_admin=True)

    # Resolve user
    user = _resolve(token=token, api_key=key)
    if not user:
        raise HTTPException(401, "Authentication required")

    return AuthContext(
        api_key_id=None if user.is_superadmin else str(user.id),
        is_admin=user.is_superadmin,
    )


def require_admin(
    auth: AuthContext = Depends(get_auth_context),
) -> AuthContext:
    """api_chat backward compat: require admin context."""
    if not auth.is_admin:
        raise HTTPException(403, "Admin access required")
    return auth
