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

V3_GRANTS_REPLACE_STRIPE = """
-- Payments now happen on the website; Railway only receives server-to-server credit grants.
DROP TABLE IF EXISTS stripe_events;
DROP INDEX IF EXISTS credit_ledger_purchase_once;
ALTER TABLE credit_ledger DROP COLUMN IF EXISTS purchase_id;
DROP TABLE IF EXISTS credit_purchases;

-- One row per payment the website reports. payment_id is the website's unique ID
-- (e.g. a Stripe Checkout session or PaymentIntent ID); a payment can be granted only once.
CREATE TABLE credit_grants (
    payment_id   TEXT PRIMARY KEY,
    customer_id  TEXT NOT NULL REFERENCES customers(id),
    credits      BIGINT NOT NULL CHECK (credits > 0),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX credit_grants_customer_idx ON credit_grants (customer_id, created_at DESC);

ALTER TABLE credit_ledger ADD COLUMN payment_id TEXT REFERENCES credit_grants(payment_id);
ALTER TABLE credit_ledger DROP CONSTRAINT credit_ledger_kind_check;
ALTER TABLE credit_ledger ADD CONSTRAINT credit_ledger_kind_check
    CHECK (kind IN ('reserve', 'release', 'grant', 'adjustment'));
CREATE UNIQUE INDEX credit_ledger_grant_once ON credit_ledger (payment_id) WHERE kind = 'grant';
"""

V4_CREDIT_LOTS_AND_EXPIRATION = """
-- Grants now say what they are and when they were bought. Existing grants keep their terms:
-- no purchase timestamp and no expiry (see GET /v1/admin/credits/expiry-report).
ALTER TABLE credit_grants ADD COLUMN grant_type TEXT NOT NULL DEFAULT 'paid'
    CHECK (grant_type IN ('paid', 'welcome'));
ALTER TABLE credit_grants ADD COLUMN purchased_at TIMESTAMPTZ;
ALTER TABLE credit_grants ADD COLUMN expires_at TIMESTAMPTZ;

-- A lot is one batch of credits with its own expiry. The balance is the sum of lot remainders.
--   welcome / adjustment: free credits, spent first, no expiry
--   paid: purchased credits, spent earliest-expiring first, expire 1 calendar year after purchase
--   legacy: balances that existed before lots; paid tier, no expiry (their terms are unchanged)
CREATE TABLE credit_lots (
    id           BIGSERIAL PRIMARY KEY,
    customer_id  TEXT NOT NULL REFERENCES customers(id),
    source       TEXT NOT NULL CHECK (source IN ('paid', 'welcome', 'adjustment', 'legacy')),
    payment_id   TEXT UNIQUE REFERENCES credit_grants(payment_id),
    granted_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ,                   -- NULL: never expires
    original     BIGINT NOT NULL CHECK (original > 0),
    remaining    BIGINT NOT NULL CHECK (remaining >= 0 AND remaining <= original),
    expired_at   TIMESTAMPTZ                    -- when its unspent credits were written off
);
CREATE INDEX credit_lots_customer_idx ON credit_lots (customer_id) WHERE remaining > 0;
CREATE INDEX credit_lots_expiry_idx ON credit_lots (expires_at) WHERE remaining > 0 AND expires_at IS NOT NULL;

-- Preserve every existing balance exactly, as one non-expiring legacy lot per customer.
INSERT INTO credit_lots (customer_id, source, original, remaining)
SELECT customer_id, 'legacy', balance, balance FROM credit_accounts WHERE balance > 0;

-- Which lots each reservation drew from, in spend order (refunds go back to the same lots).
CREATE TABLE credit_reservation_lots (
    reservation_id  UUID NOT NULL REFERENCES credit_reservations(id),
    seq             INTEGER NOT NULL,
    lot_id          BIGINT NOT NULL REFERENCES credit_lots(id),
    amount          BIGINT NOT NULL CHECK (amount > 0),
    PRIMARY KEY (reservation_id, seq)
);

-- Credits a refund could not return because their lot had expired by settlement time.
ALTER TABLE credit_reservations ADD COLUMN forfeited INTEGER NOT NULL DEFAULT 0 CHECK (forfeited >= 0);

ALTER TABLE credit_ledger ADD COLUMN lot_id BIGINT REFERENCES credit_lots(id);
ALTER TABLE credit_ledger DROP CONSTRAINT credit_ledger_kind_check;
ALTER TABLE credit_ledger ADD CONSTRAINT credit_ledger_kind_check
    CHECK (kind IN ('reserve', 'release', 'grant', 'adjustment', 'expire'));
-- A refund can now touch several lots: once per (reservation, lot).
DROP INDEX credit_ledger_release_once;
CREATE UNIQUE INDEX credit_ledger_release_once ON credit_ledger (reservation_id, lot_id) WHERE kind = 'release';
-- A lot expires once.
CREATE UNIQUE INDEX credit_ledger_expire_once ON credit_ledger (lot_id) WHERE kind = 'expire';
"""

V5_GRANT_PURCHASE_TIME_SOURCE = """
-- Where a paid grant's purchase time came from: 'verified' (sent by the website) or 'received'
-- (not sent; Railway's receive time was used). NULL for grants from before expiration existed.
ALTER TABLE credit_grants ADD COLUMN purchased_at_source TEXT
    CHECK (purchased_at_source IN ('verified', 'received'));
"""

V6_USAGE_CLARIFICATIONS = """
-- Plain-English requests can be answered with a clarifying question (never charged).
ALTER TABLE usage_operations DROP CONSTRAINT IF EXISTS usage_operations_status_check;
ALTER TABLE usage_operations ADD CONSTRAINT usage_operations_status_check
    CHECK (status IN ('ok', 'error', 'needs_clarification'));
"""

V7_GITHUB_IMPORTS = """
-- Customer self-service imports of public GitHub repos. Private to the customer that submitted
-- them; one import per (customer, repo, commit).
CREATE TABLE github_imports (
    id              UUID PRIMARY KEY,
    customer_id     TEXT NOT NULL REFERENCES customers(id),
    repo_url        TEXT NOT NULL,
    repo_full_name  TEXT NOT NULL,
    commit_sha      TEXT NOT NULL CHECK (commit_sha ~ '^[0-9a-f]{40}$'),
    entry_point     TEXT,
    status          TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'ready', 'failed')),
    stage           TEXT NOT NULL DEFAULT 'queued',
    failure_code    TEXT,
    failure_reason  TEXT,
    failure_details JSONB,
    operation       TEXT,
    manifest        JSONB,
    spec            JSONB,
    pins            JSONB,
    license_spdx    TEXT,
    license_text    TEXT,
    example_response JSONB,
    attempts        INTEGER NOT NULL DEFAULT 0,
    claimed_by      UUID,
    claimed_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    ready_at        TIMESTAMPTZ
);
CREATE UNIQUE INDEX github_imports_once ON github_imports (customer_id, lower(repo_full_name), commit_sha);
CREATE UNIQUE INDEX github_imports_operation ON github_imports (customer_id, operation)
    WHERE operation IS NOT NULL AND status IN ('running', 'ready');
CREATE INDEX github_imports_queue ON github_imports (status, created_at);
CREATE INDEX github_imports_customer ON github_imports (customer_id, created_at DESC);

-- Short-lived keys Cognex mints to verify an import through /v1/execute (never billed, never listed).
ALTER TABLE api_keys ADD COLUMN kind TEXT NOT NULL DEFAULT 'customer' CHECK (kind IN ('customer', 'import_check'));
ALTER TABLE api_keys ADD COLUMN scope TEXT;
"""

V8_PUBLISHED_IMPORTS = """
-- Imports added by the Cognex operator can be published under a Cognex name: callable by every
-- customer through POST /v1/execute. published_name reserves the name (also while the import is
-- still running); the operation is live only when published_at is set and the import is ready.
ALTER TABLE github_imports ADD COLUMN published_name TEXT;
ALTER TABLE github_imports ADD COLUMN published_summary TEXT;
ALTER TABLE github_imports ADD COLUMN published_at TIMESTAMPTZ;
CREATE UNIQUE INDEX github_imports_published_name ON github_imports (lower(published_name))
    WHERE published_name IS NOT NULL;
"""

V9_PUBLISHED_WHEN_TO_USE = """
-- Discovery: when an app or AI builder should pick a published operation (GET /v1/operations).
ALTER TABLE github_imports ADD COLUMN published_when_to_use TEXT;
"""

MIGRATIONS: List[Tuple[int, str, str]] = [
    (1, "accounts_keys_usage", V1_ACCOUNTS_KEYS_USAGE),
    (2, "prepaid_credits", V2_PREPAID_CREDITS),
    (3, "grants_replace_stripe", V3_GRANTS_REPLACE_STRIPE),
    (4, "credit_lots_and_expiration", V4_CREDIT_LOTS_AND_EXPIRATION),
    (5, "grant_purchase_time_source", V5_GRANT_PURCHASE_TIME_SOURCE),
    (6, "usage_clarifications", V6_USAGE_CLARIFICATIONS),
    (7, "github_imports", V7_GITHUB_IMPORTS),
    (8, "published_imports", V8_PUBLISHED_IMPORTS),
    (9, "published_when_to_use", V9_PUBLISHED_WHEN_TO_USE),
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
