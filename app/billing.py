"""Prepaid credits: balances, grants from the website, and per-operation charging.

Payments happen on the website. After the website's server confirms a payment, it calls
POST /v1/credits/grant with the customer ID, credit amount and its unique payment ID. A payment
ID is granted at most once (primary key + unique ledger index), so retries are safe.

Money rules
- One balance per customer (credit_accounts), shared by all its keys, never negative.
- Every change is a credit_ledger row; balance == sum(delta).
- /v1/execute reserves the whole batch cost in one transaction (row lock on the balance),
  then settles to the number of operations that actually started; the rest is released.
- Reservations of a crashed/restarted API process are released by the sweeper, charging
  only operations recorded as started.

BILLING_MODE: off (default; /v1/execute charges nothing) | on (customer keys are charged and
blocked at 0). Grants and balances work in both modes, so balances can be loaded before
enforcement is switched on.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import uuid
from typing import Any, Dict, Optional

from app import db
from app.accounts import KeyOwner

log = logging.getLogger("uvicorn.error")

INSTANCE_ID = uuid.uuid4()
HEARTBEAT_SECONDS = 15
STALE_INSTANCE_SECONDS = 60
GRANT_SECRET_ENV = "CREDITS_GRANT_SECRET"
PAYMENT_ID_RE = re.compile(r"^[\x21-\x7e]{1,255}$")   # printable ASCII, no spaces (Stripe IDs fit)
MAX_GRANT = 1_000_000_000


def mode() -> str:
    value = (os.environ.get("BILLING_MODE") or "off").strip().lower()
    return value if value in ("off", "on") else "off"


def enforced() -> bool:
    return mode() == "on"


class BillingError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status, self.code, self.extra = status, code, extra


# -- balances & ledger -----------------------------------------------------------------------
def _ensure_account(conn, customer_id: str) -> None:
    conn.execute("INSERT INTO customers (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (customer_id,))
    conn.execute("INSERT INTO credit_accounts (customer_id) VALUES (%s) ON CONFLICT (customer_id) DO NOTHING",
                 (customer_id,))


def _apply(conn, customer_id: str, delta: int, kind: str, *, reservation_id=None, payment_id=None,
           note: Optional[str] = None) -> int:
    """Change a balance and write its ledger row. Caller holds the transaction."""
    row = conn.execute("UPDATE credit_accounts SET balance = balance + %s, updated_at = now() "
                       "WHERE customer_id = %s RETURNING balance", (delta, customer_id)).fetchone()
    conn.execute("INSERT INTO credit_ledger (customer_id, delta, kind, reservation_id, payment_id, note, balance_after) "
                 "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                 (customer_id, delta, kind, reservation_id, payment_id, note, row[0]))
    return row[0]


def balance_summary(customer_id: str, recent: int = 20) -> Dict[str, Any]:
    with db.pool().connection() as conn:
        row = conn.execute("SELECT balance FROM credit_accounts WHERE customer_id = %s", (customer_id,)).fetchone()
        held = conn.execute("SELECT COALESCE(sum(amount), 0) FROM credit_reservations "
                            "WHERE customer_id = %s AND status = 'held'", (customer_id,)).fetchone()[0]
        ledger = conn.execute("SELECT kind, delta, balance_after, created_at, payment_id "
                              "FROM credit_ledger WHERE customer_id = %s ORDER BY id DESC LIMIT %s",
                              (customer_id, recent)).fetchall()
        grants = conn.execute("SELECT payment_id, credits, created_at FROM credit_grants "
                              "WHERE customer_id = %s ORDER BY created_at DESC LIMIT 10", (customer_id,)).fetchall()
    return {
        "balance": row[0] if row else 0,
        "held": int(held),  # reserved by requests running right now (already deducted from balance)
        "billing_mode": mode(),
        "recent_ledger": [{"kind": k, "delta": d, "balance_after": b, "created_at": t, "payment_id": p}
                          for k, d, b, t, p in ledger],
        "recent_grants": [{"payment_id": p, "credits": c, "created_at": t} for p, c, t in grants],
    }


def grant(customer_id: str, credits: int, payment_id: str) -> Dict[str, Any]:
    """Add credits for one website payment, exactly once per payment_id.

    Replaying the same payment returns already_credited; reusing a payment_id with a different
    customer or amount is refused (409) and changes nothing.
    """
    with db.pool().connection() as conn:
        _ensure_account(conn, customer_id)
        # Concurrent calls with one payment_id serialize on the primary key; only one inserts.
        first = conn.execute("INSERT INTO credit_grants (payment_id, customer_id, credits) VALUES (%s, %s, %s) "
                             "ON CONFLICT (payment_id) DO NOTHING RETURNING payment_id",
                             (payment_id, customer_id, credits)).fetchone()
        if first is None:
            prior = conn.execute("SELECT customer_id, credits FROM credit_grants WHERE payment_id = %s",
                                 (payment_id,)).fetchone()
            if prior != (customer_id, credits):
                raise BillingError(409, "payment_id_conflict",
                                   "this payment_id was already granted with a different customer or amount")
            bal = conn.execute("SELECT balance FROM credit_accounts WHERE customer_id = %s", (customer_id,)).fetchone()[0]
            return {"status": "already_credited", "customer_id": customer_id, "credits": credits,
                    "payment_id": payment_id, "balance": bal}
        bal = _apply(conn, customer_id, credits, "grant", payment_id=payment_id)
    log.info("granted %d credits for a website payment", credits)
    return {"status": "credited", "customer_id": customer_id, "credits": credits,
            "payment_id": payment_id, "balance": bal}


def adjust(customer_id: str, delta: int, note: str) -> int:
    """Operator adjustment (support grants, corrections). Cannot take a balance below 0."""
    with db.pool().connection() as conn:
        _ensure_account(conn, customer_id)
        bal = conn.execute("SELECT balance FROM credit_accounts WHERE customer_id = %s FOR UPDATE",
                           (customer_id,)).fetchone()[0]
        if bal + delta < 0:
            raise BillingError(409, "balance_would_go_negative", "adjustment would make the balance negative",
                               balance=bal)
        return _apply(conn, customer_id, delta, "adjustment", note=note[:200])


# -- reservations ----------------------------------------------------------------------------
class Reservation:
    """Credits held for one batch. mark_started() is called as each operation starts."""

    def __init__(self, rid: uuid.UUID, owner: KeyOwner, amount: int):
        self.id, self.owner, self.amount = rid, owner, amount
        self.started = 0
        self._lock = threading.Lock()

    def mark_started(self) -> None:
        with self._lock:
            if self.started >= self.amount:
                return
            self.started += 1
        try:  # persisted so a crash recovery charges started work; in-memory count stays authoritative
            with db.pool().connection() as conn:
                conn.execute("UPDATE credit_reservations SET started = LEAST(started + 1, amount) "
                             "WHERE id = %s AND status = 'held'", (self.id,))
        except Exception as exc:
            log.warning("could not persist start for reservation %s: %s", self.id, type(exc).__name__)


def reserve(owner: KeyOwner, amount: int, max_seconds: float) -> Reservation:
    """Atomically hold `amount` credits or raise BillingError(402, insufficient_credits)."""
    rid = uuid.uuid4()
    with db.pool().connection() as conn:
        # Make sure this process is registered (the maintenance thread keeps its heartbeat fresh).
        conn.execute("INSERT INTO api_instances (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (INSTANCE_ID,))
        _ensure_account(conn, owner.customer_id)
        bal = conn.execute("SELECT balance FROM credit_accounts WHERE customer_id = %s FOR UPDATE",
                           (owner.customer_id,)).fetchone()[0]
        if bal < amount:
            raise BillingError(402, "insufficient_credits",
                               f"this request needs {amount} credit(s) but the balance is {bal}",
                               required=amount, balance=bal)
        conn.execute("INSERT INTO credit_reservations (id, customer_id, key_id, instance_id, amount, expires_at) "
                     "VALUES (%s, %s, %s, %s, %s, now() + make_interval(secs => %s))",
                     (rid, owner.customer_id, owner.key_id, INSTANCE_ID, amount, max_seconds))
        _apply(conn, owner.customer_id, -amount, "reserve", reservation_id=rid)
    return Reservation(rid, owner, amount)


def _release(conn, rid, customer_id: str, amount: int, charged: int, status: str) -> None:
    refund = amount - charged
    if refund > 0:
        _apply(conn, customer_id, refund, "release", reservation_id=rid)
    conn.execute("UPDATE credit_reservations SET status = %s, charged = %s, started = %s, settled_at = now() "
                 "WHERE id = %s", (status, charged, charged, rid))


def settle(res: Reservation) -> int:
    """Charge started operations, release the rest. Idempotent. Returns credits charged."""
    with db.pool().connection() as conn:
        row = conn.execute("SELECT status, amount, charged FROM credit_reservations WHERE id = %s FOR UPDATE",
                           (res.id,)).fetchone()
        if row[0] != "held":  # already released by the sweeper
            return row[2] or 0
        charged = min(res.started, row[1])
        _release(conn, res.id, res.owner.customer_id, row[1], charged, "settled")
    return charged


# -- crash recovery --------------------------------------------------------------------------
def heartbeat() -> None:
    with db.pool().connection() as conn:
        conn.execute("INSERT INTO api_instances (id) VALUES (%s) ON CONFLICT (id) DO UPDATE SET heartbeat_at = now()",
                     (INSTANCE_ID,))


def sweep() -> int:
    """Release reservations whose API process died (stale heartbeat) or that outlived their expiry.

    Operations recorded as started are charged; everything else is refunded.
    """
    released = 0
    with db.pool().connection() as conn:
        rows = conn.execute(
            "SELECT r.id, r.customer_id, r.amount, r.started FROM credit_reservations r "
            "JOIN api_instances i ON i.id = r.instance_id "
            "WHERE r.status = 'held' AND (i.heartbeat_at < now() - make_interval(secs => %s) OR r.expires_at < now()) "
            "ORDER BY r.created_at LIMIT 200 FOR UPDATE OF r SKIP LOCKED",
            (STALE_INSTANCE_SECONDS,)).fetchall()
        for rid, customer_id, amount, started in rows:
            _release(conn, rid, customer_id, amount, started, "released")
            released += 1
    if released:
        log.warning("released %d orphaned credit reservation(s)", released)
    return released


class Maintenance(threading.Thread):
    """Heartbeat + sweep loop for this API process."""

    def __init__(self):
        super().__init__(daemon=True, name="billing-maintenance")
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                heartbeat()
                sweep()
            except Exception as exc:
                log.warning("billing maintenance failed: %s", type(exc).__name__)
            self.stop.wait(HEARTBEAT_SECONDS)
