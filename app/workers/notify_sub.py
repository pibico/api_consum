"""Family-notify subscriber — the server end of the edge uplink.

CM4 gateways forward actionable events (threshold alerts, left-on) to
``home/<client>/gateway_<host>/event`` (see cm4-consumia poller/notify.py).
This worker subscribes and stores them in SQLite; the fan-out to a relative
or carer (email/SMS per household contact preferences) hangs off this table.

Runs on a daemon thread inside the app lifespan; a missing broker config
just disables it (the landing must never depend on MQTT being up).
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from typing import Optional, Set

from app.core.config import settings

log = logging.getLogger("consum.notify")

# Tenant allow-list snapshot: the broker uses a single shared credential, so a
# publisher can forge home/<any-slug>/... — validate the client slug against
# the api_auth-synced customers registry. This worker is a plain daemon thread
# (not async), so it can't use the service's async pool; a short sync psycopg
# query refreshed on a TTL is the cheap, thread-safe way to keep the snapshot.
_slug_cache: Set[str] = set()
_slug_cache_ts: float = 0.0
_SLUG_TTL = 300.0
_unknown_log_ts: dict[str, float] = {}
_UNKNOWN_LOG_WINDOW = 3600.0   # WARN at most once/hour per unknown slug


def _valid_slugs() -> Optional[Set[str]]:
    """Cached set of known customer slugs (None if the registry is unreachable
    — then we do NOT flag, to avoid false positives during a DB blip)."""
    global _slug_cache, _slug_cache_ts
    now = time.time()
    if _slug_cache and now - _slug_cache_ts < _SLUG_TTL:
        return _slug_cache
    try:
        import psycopg
        with psycopg.connect(
            host=settings.TS_DB_HOST, port=settings.TS_DB_PORT,
            dbname=settings.TS_DB_NAME, user=settings.TS_DB_USER,
            password=settings.TS_DB_PASSWORD, connect_timeout=5,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT slug FROM customers")
                _slug_cache = {r[0] for r in cur.fetchall()}
                _slug_cache_ts = now
        return _slug_cache
    except Exception as exc:
        log.warning("could not refresh customer slug snapshot: %s", exc)
        return _slug_cache or None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS edge_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received_ts INTEGER NOT NULL,
    topic TEXT NOT NULL,
    client TEXT,
    gateway TEXT,
    ts INTEGER,
    kind TEXT,
    severity TEXT,
    title TEXT,
    mac TEXT,
    ch TEXT,
    value REAL,
    detail TEXT,
    notified INTEGER DEFAULT 0,
    unknown_client INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_edge_events_ts ON edge_events(received_ts);
"""

# Additive column for pre-existing DBs (executescript won't ALTER an existing
# table). Ignored once the column exists.
_MIGRATE = "ALTER TABLE edge_events ADD COLUMN unknown_client INTEGER DEFAULT 0"


def _store(topic: str, payload: bytes) -> None:
    parts = topic.split("/")
    client = parts[1] if len(parts) > 1 else None
    gateway = parts[2] if len(parts) > 2 else None
    try:
        d = json.loads(payload.decode("utf-8", "replace"))
    except Exception:
        d = {}
    # Tenant check: flag (don't drop — this is the only record of the event and
    # there is no downstream yet, so dropping would be silent data loss).
    unknown = 0
    slugs = _valid_slugs()
    if slugs is not None and client and client not in slugs:
        unknown = 1
        now = time.time()
        if now - _unknown_log_ts.get(client, 0.0) > _UNKNOWN_LOG_WINDOW:
            _unknown_log_ts[client] = now
            log.warning("edge event from UNKNOWN client slug %r (topic %s) — "
                        "flagged, broker credential likely shared", client, topic)
    c = sqlite3.connect(settings.DB_PATH, timeout=10)
    try:
        c.executescript(_SCHEMA)
        try:
            c.execute(_MIGRATE)
        except sqlite3.OperationalError:
            pass   # column already exists
        c.execute(
            "INSERT INTO edge_events (received_ts,topic,client,gateway,ts,kind,"
            "severity,title,mac,ch,value,detail,unknown_client) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (int(time.time()), topic, client, gateway, d.get("ts"),
             d.get("kind"), d.get("severity"), d.get("title"), d.get("mac"),
             d.get("ch"), d.get("value"), json.dumps(d.get("detail") or {}), unknown))
        c.commit()
    finally:
        c.close()
    log.info("edge event stored: %s %s %s%s", topic, d.get("kind"),
             d.get("title"), " [UNKNOWN]" if unknown else "")


def start() -> None:
    """Fire up the subscriber thread; no-op when MQTT is unconfigured."""
    if not settings.MQTT_BROKER:
        log.info("family-notify subscriber disabled (MQTT_BROKER not set)")
        return

    def _run() -> None:
        import paho.mqtt.client as mqtt
        try:
            from paho.mqtt.client import CallbackAPIVersion
            c = mqtt.Client(CallbackAPIVersion.VERSION2,
                            client_id="api-consum-notify")
        except (ImportError, AttributeError):
            c = mqtt.Client(client_id="api-consum-notify")
        if settings.MQTT_USER:
            c.username_pw_set(settings.MQTT_USER, settings.MQTT_PASS)

        def on_connect(client, userdata, flags, reason_code, properties=None):
            log.info("MQTT connected (%s) — subscribing %s",
                     reason_code, settings.MQTT_EVENT_TOPIC)
            client.subscribe(settings.MQTT_EVENT_TOPIC, qos=1)

        def on_message(client, userdata, msg):
            try:
                _store(msg.topic, msg.payload)
            except Exception as exc:
                log.warning("event store failed: %s", exc)

        c.on_connect = on_connect
        c.on_message = on_message
        c.reconnect_delay_set(min_delay=1, max_delay=120)
        while True:
            try:
                c.connect(settings.MQTT_BROKER, settings.MQTT_PORT, 30)
                c.loop_forever(retry_first_connection=True)
            except Exception as exc:
                log.warning("MQTT loop error: %s — retrying in 30 s", exc)
                time.sleep(30)

    threading.Thread(target=_run, daemon=True, name="consum-notify").start()
