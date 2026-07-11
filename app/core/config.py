"""api_consum settings — .env-overridable, nothing hardcoded.

F0 (2026-07-11): extended for the product app — api_auth remote validation,
shared Timescale (read-only), api_exo/api_chat service clients. Follows the
api_exo/api_edge conventions exactly (AUTH_BASE_URL, API_KEY≠ADMIN_API_KEY,
validate-cache TTLs, no hardcoded box domains).
"""
from pathlib import Path
from typing import List

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    PROJECT_NAME: str = "CONSUM-IA"
    DESCRIPTION: str = ("CONSUM-IA server side — public landing, the household "
                        "product app (dashboard + OE3) and the family-notify "
                        "subscriber.")
    VERSION: str = "0.2.0"
    API_V1_STR: str = "/api/v1"

    HOST: str = "0.0.0.0"
    PORT: int = 8183
    ROOT_PATH: str = ""

    BASE_DIR: Path = Path(__file__).resolve().parent.parent.parent
    DB_PATH: Path = Path(__file__).resolve().parent.parent.parent / "consum.db"

    # ── Auth — api_auth remote validation (ADR-9: no local JWT secret) ──
    API_KEY: str = ""          # regular service key (non-admin; e.g. handed to peers)
    ADMIN_API_KEY: str = ""    # local superadmin escape hatch — MUST differ from API_KEY
    AUTH_BASE_URL: str = Field(
        default="https://app.pibico.es/auth",
        validation_alias=AliasChoices("AUTH_BASE_URL", "API_AUTH_URL"),
    )
    AUTH_VALIDATE_CACHE_TTL: int = 90
    AUTH_VALIDATE_NEGATIVE_CACHE_TTL: int = 10

    # ── CORS (values in .env; never "*": allow_credentials=True) ──
    CORS_ORIGINS: List[str] = []

    # ── Shared TimescaleDB (pibiconnect_ts) — READ ONLY role ──
    TS_DB_HOST: str = "127.0.0.1"
    TS_DB_PORT: int = 5433
    TS_DB_NAME: str = "pibiconnect_ts"
    TS_DB_USER: str = "api_consum"
    TS_DB_PASSWORD: str = ""
    TS_POOL_MIN: int = 1
    TS_POOL_MAX: int = 5

    # ── Peer services (per-box, .env) ──
    EXO_BASE_URL: str = "https://app.pibico.es/apiexo"
    EXO_API_KEY: str = ""      # api_exo's regular service key (reads are gated)
    EDGE_BASE_URL: str = "https://app.pibico.es/apiedge"
    EDGE_API_KEY: str = ""     # api_edge service key (device listing)
    CHAT_BASE_URL: str = "https://api.pibico.es/chat"
    CHAT_API_KEY: str = ""     # api_chat service key (AI narrative, F4)

    # ── Household default location (OE3 climate cross) — v1 single-site;
    #    per-customer coords come later with the org onboarding flow ──
    DEFAULT_LAT: float = 43.36
    DEFAULT_LON: float = -5.84

    # ── MQTT uplink (family-notify subscriber, unchanged) ──
    MQTT_BROKER: str = ""
    MQTT_PORT: int = 1883
    MQTT_USER: str = ""
    MQTT_PASS: str = ""
    MQTT_EVENT_TOPIC: str = "home/+/+/event"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"


settings = Settings()
