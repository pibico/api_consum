"""api_consum RBAC — org membership, customer scoping and TIERS.

Unlike api_edge/api_exo (superadmin consoles), api_consum is the PRODUCT app:
ordinary org members log in and see THEIR household(s). The identity chain is

    user (api_auth JWT) → orgs[] with ServiceAccess 'api_consum'
        → org.customers[].slug  (physical tenants, ADR-10)
        → customer_id in the shared Timescale (customers table)
        → devices → sensor_data

Everything here derives from the api_auth `/validate` payload, which already
carries per-org `service_roles['api_consum'] = {role, plan, ai_enabled}` and
`customers = [{slug, display_name, is_primary}]` (validate.py:95-126) — no
extra round-trips.

Tier model (ServiceAccess.plan): basic | pro | enterprise.
  - basic      → dashboard + OE3 básico
  - pro        → + predicción de factura, desglose avanzado, export
  - enterprise → pro + (futuro) multi-hogar/flota
`ai_enabled` (bool) gates the AI narrative/chat independently of plan.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from fastapi import Depends, HTTPException, Request

from app.core.auth import _service_key_user, _validate_jwt
from app.core.config import settings

SERVICE = "api_consum"

_TIER_RANK = {"basic": 0, "pro": 1, "enterprise": 2}
_ROLE_RANK = {"viewer": 0, "member": 1, "editor": 2, "admin": 3, "owner": 4}


@dataclass
class ConsumContext:
    """Resolved caller context for one request."""
    user: dict                      # /validate payload (or synthetic for keys)
    is_superadmin: bool = False
    is_service: bool = False        # X-API-Key caller (peer service)
    orgs: List[dict] = field(default_factory=list)   # orgs WITH api_consum access
    customer_slugs: Set[str] = field(default_factory=set)
    tier: str = "basic"             # highest plan across the caller's orgs
    ai_enabled: bool = False
    role: str = "viewer"            # highest effective role for api_consum
    # Per-slug role/tier in the OWNING org — the authoritative basis for
    # write/pro gates. `role`/`tier` above are the flattened global MAX (a fast
    # coarse gate) and MUST NOT be trusted for a specific customer: a user who
    # is owner/pro in org A but member/basic in org B would otherwise escalate
    # onto org B's slug. Always resolve per-slug when a slug is in play.
    slug_role: Dict[str, str] = field(default_factory=dict)
    slug_tier: Dict[str, str] = field(default_factory=dict)

    def check_slug(self, slug: str, *, min_role: Optional[str] = None,
                   min_tier: Optional[str] = None) -> str:
        """403 unless the caller may access this customer slug — and, when
        `min_role`/`min_tier` are given, unless the caller meets them IN THE ORG
        THAT OWNS THIS SLUG (not the flattened global max)."""
        if self.is_superadmin or self.is_service:
            return slug
        if slug not in self.customer_slugs:
            raise HTTPException(403, detail=f"customer '{slug}' is not in your organization")
        if min_role is not None:
            have = self.slug_role.get(slug, "viewer")
            if _ROLE_RANK.get(have, 0) < _ROLE_RANK.get(min_role, 99):
                raise HTTPException(403, detail={
                    "code": "ROLE_REQUIRED", "required": min_role, "current": have,
                    "message": f"Esta acción requiere el rol {min_role}."})
        if min_tier is not None:
            have = self.slug_tier.get(slug, "basic")
            if _TIER_RANK.get(have, 0) < _TIER_RANK.get(min_tier, 99):
                raise HTTPException(403, detail={
                    "code": "TIER_REQUIRED", "required": min_tier, "current": have,
                    "message": f"Esta función requiere el plan {min_tier}."})
        return slug

    def tier_for(self, slug: str) -> str:
        """Effective plan in the org owning `slug` (enterprise for superadmin)."""
        if self.is_superadmin or self.is_service:
            return "enterprise"
        return self.slug_tier.get(slug, "basic")


def _bearer_token(request: Request) -> Optional[str]:
    auth = request.headers.get("authorization") or ""
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.cookies.get("auth_jwt")


def _build_context(payload: dict) -> ConsumContext:
    """Reduce the /validate payload to the api_consum view of the world."""
    ctx = ConsumContext(user=payload, is_superadmin=bool(payload.get("is_superadmin")))
    best_tier, best_role, ai = 0, 0, False
    for org in payload.get("orgs", []):
        if SERVICE not in (org.get("services") or []):
            continue
        sr = (org.get("service_roles") or {}).get(SERVICE) or {}
        plan = (sr.get("plan") or "basic").lower()
        if plan == "free":
            plan = "basic"
        role = (sr.get("role") or org.get("role") or "viewer").lower()
        ai = ai or bool(sr.get("ai_enabled"))
        best_tier = max(best_tier, _TIER_RANK.get(plan, 0))
        best_role = max(best_role, _ROLE_RANK.get(role, 0))
        ctx.orgs.append(org)
        for c in org.get("customers") or []:
            slug = (c.get("slug") or "").strip()
            if slug:
                ctx.customer_slugs.add(slug)
                # Slugs are globally unique (one org per customer) → last write
                # wins harmlessly; record this org's role/plan for the slug.
                ctx.slug_role[slug] = role
                ctx.slug_tier[slug] = plan
    ctx.tier = [k for k, v in _TIER_RANK.items() if v == best_tier][0]
    ctx.role = [k for k, v in _ROLE_RANK.items() if v == best_role][0]
    ctx.ai_enabled = ai or ctx.is_superadmin
    if ctx.is_superadmin:
        ctx.tier = "enterprise"
    return ctx


async def consum_context(request: Request) -> ConsumContext:
    """THE dependency: resolve the caller into a ConsumContext.

    Accepts (in precedence order):
      1. X-API-Key == ADMIN_API_KEY  → superadmin service context
      2. X-API-Key == API_KEY        → non-admin service context (peer calls;
                                       unrestricted read, e.g. CM4 uplink jobs)
      3. Bearer JWT / auth_jwt cookie → member context (org-scoped)
    Raises 401 with no credential; 403 if the JWT user has NO org with
    api_consum access (mirrors api_auth's own service-membership gate).
    """
    key = request.headers.get("x-api-key") or ""
    if key and settings.ADMIN_API_KEY and key == settings.ADMIN_API_KEY:
        return ConsumContext(user={"email": "admin-key"}, is_superadmin=True,
                             is_service=True, tier="enterprise", ai_enabled=True)
    if _service_key_user(request) is not None:
        return ConsumContext(user={"email": "api-key-user"}, is_service=True,
                             tier="enterprise", ai_enabled=False)

    token = _bearer_token(request)
    if not token:
        raise HTTPException(401, detail="Authentication required")
    payload = await _validate_jwt(token)
    ctx = _build_context(payload)
    if not ctx.is_superadmin and not ctx.orgs:
        raise HTTPException(403, detail="Tu organización no tiene acceso a CONSUM-IA")
    return ctx


def require_tier(min_tier: str):
    """Endpoint gate: 403 (with upsell detail) below `min_tier`."""
    min_rank = _TIER_RANK.get(min_tier, 1)

    async def _dep(ctx: ConsumContext = Depends(consum_context)) -> ConsumContext:
        if _TIER_RANK.get(ctx.tier, 0) < min_rank:
            raise HTTPException(
                403,
                detail={"code": "TIER_REQUIRED", "required": min_tier, "current": ctx.tier,
                        "message": f"Esta función requiere el plan {min_tier}."},
            )
        return ctx

    return _dep


def require_role(min_role: str):
    """Endpoint gate: 403 below `min_role` (writes on business entities).

    Superadmin (incl. ADMIN_API_KEY) bypasses; the plain service key (API_KEY)
    stays read-only — peer services must not mutate contracts.
    """
    min_rank = _ROLE_RANK.get(min_role, 2)

    async def _dep(ctx: ConsumContext = Depends(consum_context)) -> ConsumContext:
        if ctx.is_superadmin:
            return ctx
        if ctx.is_service or _ROLE_RANK.get(ctx.role, 0) < min_rank:
            raise HTTPException(
                403,
                detail={"code": "ROLE_REQUIRED", "required": min_role, "current": ctx.role,
                        "message": f"Esta acción requiere el rol {min_role}."},
            )
        return ctx

    return _dep


def require_ai():
    """Endpoint gate for AI features: ServiceAccess.ai_enabled must be true."""
    async def _dep(ctx: ConsumContext = Depends(consum_context)) -> ConsumContext:
        if not ctx.ai_enabled:
            raise HTTPException(
                403,
                detail={"code": "AI_DISABLED",
                        "message": "La IA no está habilitada para tu organización."},
            )
        return ctx

    return _dep
