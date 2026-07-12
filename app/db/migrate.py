"""Idempotent TimescaleDB migration runner for api_consum (consum schema).

Records applied migration filenames in `_migrations` (shared ledger with
api_edge/api_exo in pibiconnect_ts — filenames are namespaced by convention).
Invoke: `python -m app.db.migrate` from the repo root, with TS_* env vars set.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg


MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def ensure_migrations_table(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS _migrations (
          filename     TEXT PRIMARY KEY,
          applied_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    conn.commit()


def applied(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT filename FROM _migrations")
        return {row[0] for row in cur.fetchall()}


def apply_one(conn: psycopg.Connection, path: Path) -> None:
    sql = path.read_text()
    print(f"  applying {path.name} ({len(sql)} bytes)...", end=" ", flush=True)
    try:
        with conn.transaction():
            conn.execute(sql)
            conn.execute(
                "INSERT INTO _migrations (filename) VALUES (%s)",
                (path.name,),
            )
        print("OK")
    except Exception as exc:
        print(f"FAIL: {exc}")
        raise


def main() -> int:
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print("No migrations found.")
        return 0

    # Migrations run as superuser so DDL + CREATE EXTENSION succeed;
    # grants inside the files hand api_consum its runtime permissions.
    pg_user = os.environ.get("TS_MIGRATE_USER", "postgres")
    dsn_str = (
        f"host={os.environ['TS_DB_HOST']} "
        f"port={os.environ.get('TS_DB_PORT', '5433')} "
        f"user={pg_user} "
        f"dbname={os.environ['TS_DB_NAME']}"
    )
    with psycopg.connect(dsn_str, autocommit=False) as conn:
        ensure_migrations_table(conn)
        done = applied(conn)
        pending = [p for p in files if p.name not in done]
        if not pending:
            print(f"Up to date ({len(files)} migrations applied).")
            return 0
        print(f"Applying {len(pending)} migration(s):")
        for p in pending:
            apply_one(conn, p)
    print("All migrations applied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
