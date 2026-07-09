"""api_consum — CONSUM-IA server side.

Public landing (consum.pibico.es) + `/api/v1` (health, edge-event feed) +
the family-notify MQTT subscriber. The edge counterpart lives in the
cm4-consumia repo (api_consum_edge).
"""
import logging
import mimetypes
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.api.v1.endpoints import health
from app.core.config import settings
from app.workers import notify_sub

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
    notify_sub.start()
    yield
    log.info("api_consum down")


app = FastAPI(title=settings.PROJECT_NAME, description=settings.DESCRIPTION,
              version=settings.VERSION, root_path=os.getenv("ROOT_PATH", ""),
              docs_url="/api/docs", redoc_url="/api/redoc",
              openapi_url="/api/openapi.json", lifespan=lifespan)

app.include_router(health.router, prefix=settings.API_V1_STR, tags=["core"])


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
