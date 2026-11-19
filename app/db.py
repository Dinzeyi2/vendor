"""Postgres access for customer accounts, API keys and usage.

Configured with DATABASE_URL (Railway: reference the Postgres service's DATABASE_URL).
Without it, the API still runs with the operator API_KEY; customer features return 503.
The schema is created on first use (idempotent, guarded by an advisory lock).
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Optional

from psycopg_pool import ConnectionPool

log = logging.getLogger("uvicorn.error")

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id          TEXT PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Keys are never deleted: revocation sets revoked_at, so usage history keeps its key.
CREATE TABLE IF NOT EXISTS api_keys (
    id            UUID PRIMARY KEY,
    customer_id   TEXT NOT NULL REFERENCES customers(id),
    name          TEXT NOT NULL,
    prefix        TEXT NOT NULL,
    key_hash      BYTEA NOT NULL UNIQUE,          -- SHA-256 of the secret; the secret is never stored
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_used_at  TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS api_keys_customer_idx ON api_keys (customer_id, created_at DESC);

-- One row per POST /v1/execute made with a customer key. No inputs, outputs or secrets.
CREATE TABLE IF NOT EXISTS usage_requests (
    id           UUID PRIMARY KEY,
    customer_id  TEXT NOT NULL REFERENCES customers(id),
    key_id       UUID NOT NULL REFERENCES api_keys(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    task_count   INTEGER NOT NULL,
    ok_count     INTEGER NOT NULL,
    error_count  INTEGER NOT NULL,
    duration_ms  DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_requests_customer_idx ON usage_requests (customer_id, created_at DESC);

-- One row per task inside a request: operation name and outcome only.
CREATE TABLE IF NOT EXISTS usage_operations (
    id           BIGSERIAL PRIMARY KEY,
    request_id   UUID NOT NULL REFERENCES usage_requests(id),
    customer_id  TEXT NOT NULL REFERENCES customers(id),
    key_id       UUID NOT NULL REFERENCES api_keys(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    operation    TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('ok', 'error')),
    error_type   TEXT,
    duration_ms  DOUBLE PRECISION NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_operations_customer_idx ON usage_operations (customer_id, created_at DESC);
CREATE INDEX IF NOT EXISTS usage_operations_request_idx ON usage_operations (request_id);
"""

_lock = threading.Lock()
_pool: Optional[ConnectionPool] = None
_schema_ready = False


class DatabaseUnavailable(RuntimeError):
    pass


def configured() -> bool:
    return bool(os.environ.get("DATABASE_URL"))


def pool() -> ConnectionPool:
    """The shared connection pool; creates it and the schema on first use."""
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
                    conn.execute("SELECT pg_advisory_xact_lock(727274)")
                    conn.execute(SCHEMA)
                _schema_ready = True
                log.info("database schema ready")
            except Exception as exc:
                raise DatabaseUnavailable(f"database not reachable: {type(exc).__name__}") from exc
    return _pool


def close() -> None:
    global _pool, _schema_ready
    with _lock:
        if _pool is not None:
            _pool.close()
        _pool, _schema_ready = None, False
