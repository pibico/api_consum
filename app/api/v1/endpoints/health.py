"""Health + edge-event endpoints.

/health stays PUBLIC (liveness + api_auth registry badge). /events exposes
household alert events → auth required AND scoped to the caller's OWN
household(s): a member must never see another tenant's alerts. Superadmin and
peer-service (X-API-Key) callers see the full feed."""
import sqlite3

from fastapi import APIRouter, Depends

from app.api.v1.dependencies.rbac import ConsumContext, consum_context
from app.core.config import settings

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "healthy", "service": settings.PROJECT_NAME,
            "version": settings.VERSION}


@router.get("/events")
async def events(limit: int = 50,
                 ctx: ConsumContext = Depends(consum_context)):
    """Latest edge events (family-notify feed), scoped to the caller's
    household(s). Superadmin/service callers get the whole feed."""
    limit = max(1, min(int(limit), 200))
    where, params = "", []
    if not (ctx.is_superadmin or ctx.is_service):
        slugs = sorted(ctx.customer_slugs)
        if not slugs:
            return {"events": []}
        where = "WHERE client IN (%s)" % ",".join("?" * len(slugs))
        params = list(slugs)
    params.append(limit)
    try:
        c = sqlite3.connect(settings.DB_PATH, timeout=5)
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT received_ts, client, gateway, ts, kind, severity, title, "
            "mac, ch, value FROM edge_events " + where +
            " ORDER BY id DESC LIMIT ?", params).fetchall()
        c.close()
        return {"events": [dict(r) for r in rows]}
    except sqlite3.OperationalError:
        return {"events": []}          # table not created yet — no events
