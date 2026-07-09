"""Health + edge-event endpoints."""
import sqlite3

from fastapi import APIRouter

from app.core.config import settings

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "healthy", "service": settings.PROJECT_NAME,
            "version": settings.VERSION}


@router.get("/events")
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
