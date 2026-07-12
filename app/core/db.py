"""TimescaleDB async connection pool for api_consum (shared pibiconnect_ts).

Vendored from api_edge, with the tenancy layer removed: exogenous data is NOT
tenant-scoped (ADR-3, no customer_id), so there is no `tenant_scoped()` GUC path —
all access goes through `raw_connection()`.

The role is SELECT-only on public.* and has DML on consum.* (its own business
schema — contracts; migration app/db/migrations/001_consum_schema.sql).
"""
from __future__ import annotations

import contextlib
import logging
from typing import AsyncIterator, Optional

import psycopg
from psycopg_pool import AsyncConnectionPool

from app.core.config import settings

logger = logging.getLogger("api_consum.db")


_pool: Optional[AsyncConnectionPool] = None


def _dsn() -> str:
    return (
        f"host={settings.TS_DB_HOST} "
        f"port={settings.TS_DB_PORT} "
        f"user={settings.TS_DB_USER} "
        f"password={settings.TS_DB_PASSWORD} "
        f"dbname={settings.TS_DB_NAME}"
    )


async def open_pool() -> AsyncConnectionPool:
    """Open the global pool. Idempotent — safe to call once per process."""
    global _pool
    if _pool is not None:
        return _pool
    _pool = AsyncConnectionPool(
        conninfo=_dsn(),
        min_size=settings.TS_POOL_MIN,
        max_size=settings.TS_POOL_MAX,
        open=False,
        kwargs={"autocommit": False},
    )
    await _pool.open()
    await _pool.wait()
    logger.info(
        "TS pool open: %s:%s/%s (min=%d max=%d)",
        settings.TS_DB_HOST, settings.TS_DB_PORT, settings.TS_DB_NAME,
        settings.TS_POOL_MIN, settings.TS_POOL_MAX,
    )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is None:
        return
    await _pool.close()
    _pool = None
    logger.info("TS pool closed")


def get_pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("TS pool not open — call open_pool() in lifespan")
    return _pool


@contextlib.asynccontextmanager
async def raw_connection() -> AsyncIterator[psycopg.AsyncConnection]:
    """A pooled connection. Exogenous data is non-tenant, so no GUC scoping."""
    pool = get_pool()
    async with pool.connection() as con:
        yield con


async def ping() -> bool:
    """Liveness probe — returns True on successful SELECT 1."""
    try:
        async with raw_connection() as con:
            await con.execute("SELECT 1")
        return True
    except Exception as exc:
        logger.error("TS ping failed: %s", exc)
        return False
