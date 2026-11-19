"""Postgres access for customer accounts, API keys, usage and credits.

Configured with DATABASE_URL (Railway: reference the Postgres service's DATABASE_URL).
Without it, the API still runs with the operator API_KEY; customer features return 503.
Pending migrations (app/migrations.py) are applied on first use.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from psycopg_pool import ConnectionPool

from app.migrations import migrate

log = logging.getLogger("uvicorn.error")

_lock = threading.Lock()
_pool: Optional[ConnectionPool] = None
_schema_ready = False


class DatabaseUnavailable(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


def pool() -> ConnectionPool:
    """The shared connection pool; creates it and applies migrations on first use."""
    global _pool, _schema_ready
    if _pool is not None and _schema_ready:
        return _pool
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise DatabaseUnavailable("DATABASE_URL is not configured")
    with _lock:
        if _pool is None:
            _pool = ConnectionPool(url, min_size=1, max_size=int(os.environ.get("DB_POOL_SIZE", "10")),
                                   open=False, kwargs={"autocommit": False}, name="api")
            _pool.open(wait=False)
        if not _schema_ready:
            try:
                with _pool.connection(timeout=15) as conn:
                    applied = migrate(conn)
                _schema_ready = True
                log.info("database schema ready (migrations applied now: %s)", applied or "none")
            except Exception as exc:
                raise DatabaseUnavailable(f"database not reachable: {type(exc).__name__}") from exc
    return _pool


def close() -> None:
    global _pool, _schema_ready
    with _lock:
        if _pool is not None:
            _pool.close()
        _pool, _schema_ready = None, False
