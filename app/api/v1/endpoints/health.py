"""Health + edge-event endpoints.

/health stays PUBLIC (liveness + api_auth registry badge). /events exposes
household alert events → auth required (JWT / service key), F0."""
import sqlite3

from fastapi import APIRouter, Depends

from app.core.auth import require_auth_always
from app.core.config import settings

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "healthy", "service": settings.PROJECT_NAME,
            "version": settings.VERSION}


@router.get("/events", dependencies=[Depends(require_auth_always)])
async def events(limit: int = 50):
    """Latest edge events received from the gateways (family-notify feed)."""
    limit = max(1, min(int(limit), 200))
    try:
        c = sqlite3.connect(settings.DB_PATH, timeout=5)
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT received_ts, client, gateway, ts, kind, severity, title, "
            "mac, ch, value FROM edge_events ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
        c.close()
        return {"events": [dict(r) for r in rows]}
    except sqlite3.OperationalError:
        return {"events": []}          # table not created yet — no events
