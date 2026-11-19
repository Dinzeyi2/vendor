"""Versioned Postgres migrations, applied in order at startup.

Applied versions are recorded in schema_migrations. Everything runs in one transaction under
an advisory lock, so concurrent instances cannot apply a migration twice. Migration 1 uses
IF NOT EXISTS because it describes tables that existed before migrations were tracked.
Never edit an applied migration; add a new one.
"""
from __future__ import annotations

from typing import List, Tuple

V1_ACCOUNTS_KEYS_USAGE = """
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

V2_PREPAID_CREDITS = """
-- One balance per customer, shared by all of its keys. Never negative.
CREATE TABLE credit_accounts (
    customer_id  TEXT PRIMARY KEY REFERENCES customers(id),
    balance      BIGINT NOT NULL DEFAULT 0 CHECK (balance >= 0),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
INSERT INTO credit_accounts (customer_id) SELECT id FROM customers;

-- Running API processes; reservations held by a process that stops heartbeating are released.
CREATE TABLE api_instances (
    id            UUID PRIMARY KEY,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    heartbeat_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Credits held for one /v1/execute batch. Settled to `started` operations; the rest is released.
CREATE TABLE credit_reservations (
    id           UUID PRIMARY KEY,
    customer_id  TEXT NOT NULL REFERENCES customers(id),
    key_id       UUID NOT NULL REFERENCES api_keys(id),
    instance_id  UUID NOT NULL REFERENCES api_instances(id),
    amount       INTEGER NOT NULL CHECK (amount > 0),
    started      INTEGER NOT NULL DEFAULT 0 CHECK (started >= 0 AND started <= amount),
    charged      INTEGER CHECK (charged >= 0 AND charged <= amount),
    status       TEXT NOT NULL DEFAULT 'held' CHECK (status IN ('held', 'settled', 'released')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    settled_at   TIMESTAMPTZ
);
CREATE INDEX credit_reservations_held_idx ON credit_reservations (instance_id, expires_at) WHERE status = 'held';

-- A Stripe Checkout purchase. Pack, credits and price are fixed when the session is created.
CREATE TABLE credit_purchases (
    id                 UUID PRIMARY KEY,
    customer_id        TEXT NOT NULL REFERENCES customers(id),
    pack_id            TEXT NOT NULL,
    credits            INTEGER NOT NULL CHECK (credits > 0),
    stripe_price_id    TEXT NOT NULL,
    stripe_session_id  TEXT UNIQUE,
    livemode           BOOLEAN NOT NULL,
    status             TEXT NOT NULL DEFAULT 'pending'
                       CHECK (status IN ('pending', 'paid', 'expired', 'failed')),
    amount_total       BIGINT,
    currency           TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    paid_at            TIMESTAMPTZ
);
CREATE INDEX credit_purchases_customer_idx ON credit_purchases (customer_id, created_at DESC);

-- Every balance change. balance = sum(delta) per customer.
CREATE TABLE credit_ledger (
    id              BIGSERIAL PRIMARY KEY,
    customer_id     TEXT NOT NULL REFERENCES customers(id),
    delta           BIGINT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('reserve', 'release', 'purchase', 'adjustment')),
    reservation_id  UUID REFERENCES credit_reservations(id),
    purchase_id     UUID REFERENCES credit_purchases(id),
    note            TEXT,
    balance_after   BIGINT NOT NULL CHECK (balance_after >= 0),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX credit_ledger_customer_idx ON credit_ledger (customer_id, id DESC);
-- Hard guarantees: a purchase credits at most once; a reservation is released at most once.
CREATE UNIQUE INDEX credit_ledger_purchase_once ON credit_ledger (purchase_id) WHERE kind = 'purchase';
CREATE UNIQUE INDEX credit_ledger_release_once ON credit_ledger (reservation_id) WHERE kind = 'release';
CREATE UNIQUE INDEX credit_ledger_reserve_once ON credit_ledger (reservation_id) WHERE kind = 'reserve';

-- Stripe events already processed (deduplicates redeliveries).
CREATE TABLE stripe_events (
    id           TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    livemode     BOOLEAN NOT NULL,
    result       TEXT NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE usage_requests ADD COLUMN credits_charged INTEGER NOT NULL DEFAULT 0;
"""

MIGRATIONS: List[Tuple[int, str, str]] = [
    (1, "accounts_keys_usage", V1_ACCOUNTS_KEYS_USAGE),
    (2, "prepaid_credits", V2_PREPAID_CREDITS),
]


def migrate(conn) -> List[int]:
    """Apply pending migrations on `conn` (caller commits). Returns versions applied now."""
    conn.execute("SELECT pg_advisory_xact_lock(727274)")
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                 " version INTEGER PRIMARY KEY, name TEXT NOT NULL,"
                 " applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations").fetchall()}
    done = []
    for version, name, sql in MIGRATIONS:
        if version not in applied:
            conn.execute(sql)
            conn.execute("INSERT INTO schema_migrations (version, name) VALUES (%s, %s)", (version, name))
            done.append(version)
    return done
