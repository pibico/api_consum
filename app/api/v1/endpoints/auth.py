"""
Auth proxy endpoints — proxies OTP login to api_auth.
Browser calls api_consum, api_consum calls api_auth server-side (no CORS issues).
Route names, cookie handling and logout semantics mirror api_edge exactly
(app/api/v1/endpoints/auth.py there) — api_edge is the reference pattern.

verify-otp also sets an httpOnly `auth_jwt` cookie (in addition to returning
the token in the JSON body, unchanged for existing localStorage-based JS) so
the server-rendered console (web.py) can gate pages by real auth state on
page navigation, not just after client-side JS runs.
"""

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel
from typing import Optional
import logging
import time

import httpx
from jose import jwt as jose_jwt

from app.core.config import settings
from app.core import http_client

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)

# Env-driven per box (AUTH_BASE_URL / legacy API_AUTH_URL) — never a hardcoded
# box domain; this repo pattern deploys on several boxes.
AUTH_BASE = f"{settings.AUTH_BASE_URL.rstrip('/')}/api/v1/auth"

COOKIE_NAME = "auth_jwt"
_DEFAULT_COOKIE_MAX_AGE = 3600  # fallback when the token has no readable `exp`


class OTPRequest(BaseModel):
  email: str


class OTPVerify(BaseModel):
  email: str
  code: str


def _cookie_max_age(token: str) -> int:
  """Best-effort: mirror the cookie's lifetime to the JWT's own `exp` claim
  (unverified peek only — signature validation happens server-side on every
  use via api_auth /validate, this is just for a sane cookie expiry)."""
  try:
    claims = jose_jwt.get_unverified_claims(token)
    exp = claims.get("exp")
    if exp:
      remaining = int(exp - time.time())
      if remaining > 0:
        return remaining
  except Exception:
    pass
  return _DEFAULT_COOKIE_MAX_AGE


@router.post("/request-otp")
async def request_otp(req: OTPRequest):
  """Proxy OTP request to api_auth."""
  try:
    client = http_client.get_client()
    r = await client.post(
      f"{AUTH_BASE}/request-otp",
      json={"email": req.email},
      timeout=15.0,
    )
    return r.json()
  except httpx.RequestError as e:
    logger.error(f"api_auth unreachable: {e}")
    raise HTTPException(503, detail="Auth service unavailable")


@router.post("/verify-otp")
async def verify_otp(req: OTPVerify, response: Response):
  """Proxy OTP verification to api_auth. Returns JWT on success and sets the
  `auth_jwt` httpOnly cookie used by the server-rendered console pages."""
  try:
    client = http_client.get_client()
    r = await client.post(
      f"{AUTH_BASE}/verify-otp",
      json={"email": req.email, "code": req.code},
      timeout=15.0,
    )
    data = r.json()

    if r.status_code != 200 or "access_token" not in data:
      raise HTTPException(r.status_code, detail=data.get("detail", "Verification failed"))

    # Fetch user info with the token
    token = data["access_token"]
    me = await client.get(
      f"{AUTH_BASE}/me",
      headers={"Authorization": f"Bearer {token}"},
      timeout=15.0,
    )

    response.set_cookie(
      key=COOKIE_NAME,
      value=token,
      max_age=_cookie_max_age(token),
      httponly=True,
      secure=True,
      samesite="lax",
      path="/",
    )

    return {
      "access_token": token,
      "token_type": "bearer",
      "user": me.json() if me.status_code == 200 else None,
    }

  except HTTPException:
    raise
  except httpx.RequestError as e:
    logger.error(f"api_auth unreachable: {e}")
    raise HTTPException(503, detail="Auth service unavailable")


def _parent_domain(request: Request) -> Optional[str]:
  """Registrable parent domain of the current host, for clearing the SHARED
  SSO cookie (e.g. app.pibico.es -> `.pibico.es`). Derived from the request
  Host (proxy-aware) — never a hardcoded box domain, so it works on every box
  this repo deploys to. Returns None for bare hosts / IPs where a
  domain-scoped cookie makes no sense."""
  host = (request.headers.get("x-forwarded-host")
          or request.headers.get("host") or "").split(":")[0].strip().lower()
  parts = [p for p in host.split(".") if p]
  if len(parts) < 2 or all(p.isdigit() for p in parts):  # IP or single-label
    return None
  return "." + ".".join(parts[-2:])


@router.post("/logout")
async def logout(request: Request, response: Response):
  """Clear the auth cookie so the page gate stops authenticating.

  Deletes `auth_jwt` on BOTH the host-only scope (set by our own verify-otp)
  AND the shared parent domain (set by api_auth during SSO — e.g. `.pibico.es`).
  Without the parent-domain delete, the SSO cookie survives and the console
  keeps letting the user straight in without re-authenticating. JS-side
  localStorage cleanup is separate (static/js/api_consum.js)."""
  response.delete_cookie(COOKIE_NAME, path="/")
  dom = _parent_domain(request)
  if dom:
    response.delete_cookie(COOKIE_NAME, path="/", domain=dom,
                           secure=True, httponly=True, samesite="lax")
  return {"status": "logged_out"}


@router.get("/validate")
async def validate_token(authorization: Optional[str] = Header(None)):
  """Proxy token validation to api_auth. `authorization` is read from the
  Authorization request header (never a query string — tokens in URLs
  leak into access logs and browser history)."""
  if not authorization:
    raise HTTPException(401, detail="Missing Authorization header")

  try:
    client = http_client.get_client()
    r = await client.get(
      f"{AUTH_BASE}/validate",
      headers={"Authorization": authorization},
      timeout=10.0,
    )
    if r.status_code == 200:
      return r.json()
    raise HTTPException(r.status_code, detail="Invalid token")

  except HTTPException:
    raise
  except httpx.RequestError as e:
    logger.error(f"api_auth unreachable: {e}")
    raise HTTPException(503, detail="Auth service unavailable")
