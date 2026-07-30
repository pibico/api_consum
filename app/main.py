"""api_consum — CONSUM-IA server side.

Public landing (consum.pibico.es) + the member-facing product app (/app:
dashboard + OE3) + `/api/v1` (health, consumption, edge-event feed) + the
family-notify MQTT subscriber. The edge counterpart lives in the
cm4-consumia repo (api_consum_edge).

F0/F1 (2026-07-11): api_auth remote auth (SSO cookie + OTP proxy), org/tier
RBAC, read-only pool over the shared Timescale, api_exo/api_edge clients.
"""
import logging
import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.api.v1.endpoints import advice as advice_endpoints
from app.api.v1.endpoints import auth as auth_endpoints
from app.api.v1.endpoints import consumption as consumption_endpoints
from app.api.v1.endpoints import contract as contract_endpoints
from app.api.v1.endpoints import invoice as invoice_endpoints
from app.api.v1.endpoints import health
from app.api.v1.endpoints import ai as ai_endpoints
from app.api.v1.endpoints import plc as plc_endpoints
from app.api.v1.endpoints import playground as playground_endpoints
from app.api.v1.endpoints import savings as savings_endpoints
from app.core import db as ts_db
from app.core import http_client
from app.core.config import settings
from app.api.v1.endpoints import anomalies as anomalies_endpoints
from app.api.v1.endpoints import absences as absences_endpoints
from app.workers import advice_scheduler, anomaly_poller, notify_sub

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("consum")

BASE = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE / "templates"))
static_path = BASE / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("api_consum %s up on :%s", settings.VERSION, settings.PORT)
    # Timescale pool — log-and-degrade: landing/auth must survive a DB outage.
    try:
        await ts_db.open_pool()
    except Exception as e:  # noqa: BLE001
        log.error("TS pool failed to open (continuing, consumption disabled): %s", e)
    notify_sub.start()
    try:
        advice_scheduler.start()
    except Exception as e:  # noqa: BLE001 — the daily/weekly email must never block startup
        log.error("advice_scheduler failed to start (continuing): %s", e)
    try:
        anomaly_poller.start()
    except Exception as e:  # noqa: BLE001 — the member anomaly feed must never block startup
        log.error("anomaly_poller failed to start (continuing): %s", e)
    yield
    advice_scheduler.shutdown()
    anomaly_poller.shutdown()
    try:
        await ts_db.close_pool()
    except Exception as e:  # noqa: BLE001
        log.error("Error closing TS pool: %s", e)
    await http_client.aclose()
    log.info("api_consum down")


app = FastAPI(title=settings.PROJECT_NAME, description=settings.DESCRIPTION,
              version=settings.VERSION, root_path=os.getenv("ROOT_PATH", settings.ROOT_PATH),
              docs_url=None, redoc_url=None,   # served locally from static/vendor (no CDN)
              openapi_url="/api/openapi.json", lifespan=lifespan)

if settings.CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.CORS_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# API routers. health stays PUBLIC (the api_auth registry badge polls
# /api/v1/health); /events (household alerts) is gated inside health.py.
app.include_router(health.router, prefix=settings.API_V1_STR, tags=["core"])
app.include_router(auth_endpoints.router, prefix=settings.API_V1_STR)
app.include_router(consumption_endpoints.router, prefix=settings.API_V1_STR)
app.include_router(savings_endpoints.router, prefix=settings.API_V1_STR)
app.include_router(contract_endpoints.router, prefix=settings.API_V1_STR)
app.include_router(invoice_endpoints.router, prefix=settings.API_V1_STR)
app.include_router(ai_endpoints.router, prefix=settings.API_V1_STR)
app.include_router(plc_endpoints.router, prefix=settings.API_V1_STR)
app.include_router(playground_endpoints.router, prefix=settings.API_V1_STR)
app.include_router(advice_endpoints.router, prefix=settings.API_V1_STR)
app.include_router(anomalies_endpoints.router, prefix=settings.API_V1_STR)
app.include_router(absences_endpoints.router, prefix=settings.API_V1_STR)

# Web HTML routes (/app product pages + SSO /login; landing stays below)
from app.api.v1.endpoints.web import router as web_router  # noqa: E402
app.include_router(web_router)


# --- Local API docs (no CDN — static/vendor, pibiCo guidelines) ---
from fastapi.openapi.docs import get_swagger_ui_html, get_redoc_html  # noqa: E402

_ROOT = os.getenv("ROOT_PATH", settings.ROOT_PATH).rstrip("/")


@app.get("/api/docs", include_in_schema=False)
async def api_docs():
    return get_swagger_ui_html(
        openapi_url=_ROOT + "/api/openapi.json",
        title="api_consum — API Docs — pibiCo",
        swagger_js_url=_ROOT + "/static/vendor/swagger-ui-bundle.js",
        swagger_css_url=_ROOT + "/static/vendor/swagger-ui.css",
    )


@app.get("/api/redoc", include_in_schema=False)
async def api_redoc():
    return get_redoc_html(
        openapi_url=_ROOT + "/api/openapi.json",
        title="api_consum — ReDoc — pibiCo",
        redoc_js_url=_ROOT + "/static/vendor/redoc.standalone.js",
    )


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def landing(request: Request):
    return templates.TemplateResponse(request, "landing.html", {
        "root_path": request.scope.get("root_path", "").rstrip("/"),
        "version": settings.VERSION,
    })


@app.get("/static/{file_path:path}", include_in_schema=False)
@app.head("/static/{file_path:path}", include_in_schema=False)
async def serve_static(file_path: str):
    full = static_path / file_path
    if full.exists() and full.is_file():
        mime, _ = mimetypes.guess_type(str(full))
        return FileResponse(full, media_type=mime)
    return JSONResponse({"error": "not found"}, status_code=404)
