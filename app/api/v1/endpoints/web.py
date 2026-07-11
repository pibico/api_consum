"""
Web HTML endpoints — Jinja2 pages with pibiCo styling.

api_consum is the PRODUCT app: unlike the api_edge/api_exo superadmin
consoles, `/app` is gated to ORG MEMBERS whose org has ServiceAccess
'api_consum' (superadmin always passes). `/` stays the public landing.
SSO login via api_auth's hosted login (`?local=1` = on-box OTP fallback).
"""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from typing import Optional
import os

from app.core.auth import _validate_jwt
from app.core.config import settings

router = APIRouter(include_in_schema=False)

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "templates")
jinja_env = Environment(
    loader=FileSystemLoader(TEMPLATES_DIR),
    autoescape=select_autoescape(["html", "xml"]),
)

STATIC_PREFIX = settings.ROOT_PATH.rstrip("/")

# Bump on every static asset change (guidelines cache-buster scheme).
ASSET_VERSION = "5"  # 5: IA page + pro tiers (CNS-F4)


def render_template(template_name: str, **context) -> str:
    template = jinja_env.get_template(template_name)
    return template.render(
        root=STATIC_PREFIX,
        root_path=STATIC_PREFIX,
        lang="es",
        version=settings.VERSION,
        auth_base_url=settings.AUTH_BASE_URL.rstrip("/") if settings.AUTH_BASE_URL else "",
        v=ASSET_VERSION,
        **context,
    )


LOGIN_URL = f"{STATIC_PREFIX}/login" if STATIC_PREFIX else "/login"
LANDING_URL = f"{STATIC_PREFIX}/" if STATIC_PREFIX else "/"
APP_URL = f"{STATIC_PREFIX}/app" if STATIC_PREFIX else "/app"


async def _require_member_page(request: Request) -> Optional[RedirectResponse]:
    """Server-side gate for the product app: any AUTHENTICATED user whose org
    has ServiceAccess 'api_consum' (or a superadmin). NOT superadmin-only —
    this is the member-facing product, unlike the edge/exo consoles.

    Redirect policy:
      - no / invalid / expired token → /login (SSO)
      - valid token but NO org with api_consum → landing `/` (they're logged
        in but not entitled; /login would loop). The landing shows contact.
    Never a raw 401/403 JSON for an HTML route.
    """
    api_key = request.query_params.get("api_key")
    if api_key and settings.ADMIN_API_KEY and api_key == settings.ADMIN_API_KEY:
        return None

    token = request.cookies.get("auth_jwt")
    if not token:
        return RedirectResponse(url=LOGIN_URL, status_code=303)

    try:
        user = await _validate_jwt(token)
    except HTTPException:
        return RedirectResponse(url=LOGIN_URL, status_code=303)

    if user.get("is_superadmin"):
        return None
    for org in user.get("orgs", []):
        if "api_consum" in (org.get("services") or []):
            return None
    return RedirectResponse(url=LANDING_URL, status_code=303)


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, local: int = 0):
    """SSO by default (api_auth hosted login sets the shared `.pibico.es`
    cookie and bounces back to /app). `?local=1` renders the on-box OTP form."""
    if local:
        return render_template("login.html")
    base = settings.AUTH_BASE_URL.rstrip("/")
    if not base:
        return render_template("login.html")
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    target = f"{proto}://{host}{STATIC_PREFIX or ''}/app"
    from urllib.parse import quote
    return RedirectResponse(url=f"{base}/?redirect={quote(target, safe='')}", status_code=302)


@router.get("/app", response_class=HTMLResponse)
async def app_home(request: Request):
    guard = await _require_member_page(request)
    if guard:
        return guard
    return render_template("app.html")


@router.get("/app/consumption", response_class=HTMLResponse)
async def consumption_page(request: Request):
    guard = await _require_member_page(request)
    if guard:
        return guard
    return render_template("consumption.html")


@router.get("/app/savings", response_class=HTMLResponse)
async def savings_page(request: Request):
    guard = await _require_member_page(request)
    if guard:
        return guard
    return render_template("savings.html")


@router.get("/app/ai", response_class=HTMLResponse)
async def ai_page(request: Request):
    guard = await _require_member_page(request)
    if guard:
        return guard
    return render_template("ai.html")
