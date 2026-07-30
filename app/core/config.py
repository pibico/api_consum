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
    # Service credential api_consum presents to api_auth's service-to-service
    # endpoints (/email/send, /email/smtp/*) — an api_auth API_KEYS entry,
    # NOT a local secret (mirrors api_edge's auth_service_api_key).
    AUTH_SERVICE_API_KEY: str = ""

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
    EXO_ADMIN_API_KEY: str = ""  # api_exo admin key — EcoFlow rule writes only
    EDGE_BASE_URL: str = "https://app.pibico.es/apiedge"
    EDGE_API_KEY: str = ""     # api_edge service key (device listing)
    CHAT_BASE_URL: str = "https://api.pibico.es/chat"
    CHAT_API_KEY: str = ""     # AIDA (LLM Chat Service) key — empty = AI off
    CHAT_PROVIDER: str = "ollama"
    CHAT_MODEL: str = ""       # e.g. "qwen2.5:14b" / "claude-haiku-4-5" — empty = AI off
    AI_ASK_DAILY_LIMIT: int = 30   # chat questions per org·day (cost control)
    # Monthly TOKEN credit per household scope (prompt+completion, all AI
    # kinds) — crossing it turns /ai/ask into 429 AI_CREDITS until the 1st.
    AI_MONTHLY_TOKEN_CAP: int = 2_000_000
    CONVERT_BASE_URL: str = "https://api.pibico.es/convert"
    CONVERT_API_KEY: str = ""  # api_convert (PDF→Markdown) key — empty = conversion off

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

    # ── Explainable forecast + advice email (Phase 1, 2026-07-28) ──
    # Dark launch: stays False until the template/content has been reviewed
    # end-to-end; mailer.send_email() short-circuits with no api_auth call.
    EMAIL_ADVICE_ENABLED: bool = False
    ADVICE_DAILY_HOUR: int = 21     # Europe/Madrid, AFTER api_exo's 20:20 prices_tomorrow job
    ADVICE_WEEKLY_DOW: int = 0      # APScheduler cron day_of_week: 0=Monday
    ADVICE_WEEKLY_HOUR: int = 8
    ADVICE_COOLDOWN_H: int = 20     # min hours between two advice emails to the same household
    ADVICE_DAILY_CAP: int = 1       # max advice emails per household per calendar day
    # Public URL for the advice email's CTA button — per-box (this service
    # has its own vhost, unlike the path-prefixed app.pibico.es services).
    PUBLIC_BASE_URL: str = "https://consum.pibico.es"

    # ── Member anomaly feed poller (UI surfacing, 2026-07-29) ──
    # Polls api_edge's Phase 2 E4 GET /anomalies with EDGE_API_KEY (already
    # set above) — disabled automatically when that key is empty.
    ANOMALY_POLL_INTERVAL_MIN: int = 15
    ANOMALY_LOOKBACK_DAYS: int = 14

    # ── Accuracy eval harness (2026-07-30, observability only) ──
    EVAL_WEEKLY_DOW: int = 0       # APScheduler cron day_of_week: 0=Monday
    EVAL_WEEKLY_HOUR: int = 5      # Europe/Madrid, before the household wakes up

    # ── Anomaly -> advice email loop closure (2026-07-30) ──
    # OWN dark-launch gate, independent of EMAIL_ADVICE_ENABLED (which
    # mailer.send_email() checks unconditionally for every template) — lets
    # the anomaly->email loop be flipped on separately from the forecast
    # cadences' rollout state. Both must be true for a real send; either
    # false short-circuits BEFORE any DB write or LLM call (see
    # anomaly_poller._dispatch_new_anomalies).
    ANOMALY_EMAIL_ENABLED: bool = False
    # Only anomaly_events at this severity (or higher) trigger an email.
    # api_edge's evaluator writes 'warning' at |z|>=ANOM_Z_THRESHOLD (default
    # 3.0) and 'critical' at |z|>=1.5x that — defaulting to 'critical' here
    # keeps routine borderline blips UI-only; only clearly extreme
    # deviations reach an inbox.
    ANOMALY_EMAIL_MIN_SEVERITY: str = "critical"

    # ── Bill-level anomaly channel (Phase 2.5, 2026-07-30) ──────────────────
    # Fires on invoice IMPORT (upload), not a poller — see bill_expectation.py
    # / bill_attr.py / bill_reconcile.py and migration 017. OWN dark-launch
    # gate (mirrors ANOMALY_EMAIL_ENABLED's relationship to EMAIL_ADVICE_
    # ENABLED): BOTH must be true for a real send.
    BILL_ANOMALY_EMAIL_ENABLED: bool = False
    # Fire when |delta_eur| >= max(BILL_ANOM_EUR_ABS_MIN, BILL_ANOM_PCT_MIN *
    # expected_eur) — spec's two-sided (over- AND under-charge) threshold.
    BILL_ANOM_EUR_ABS_MIN: float = 15.0
    BILL_ANOM_PCT_MIN: float = 0.20
    # PLC-vs-invoice reconciliation (B5): total kWh deviation beyond this
    # percentage is flagged 'meter_gap' (estimated reads are labelled
    # 'estimated_read' regardless of tolerance — never a "fault").
    RECON_PCT_TOL: float = 8.0

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False
        extra = "ignore"


settings = Settings()
