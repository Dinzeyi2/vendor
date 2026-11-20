"""Prepaid credits: lots with expiration, grants from the website, per-operation charging.

Payments happen on the website. After its server confirms a payment it calls
POST /v1/credits/grant with the customer ID, credit amount, unique payment ID and the verified
purchase timestamp. A payment ID is granted at most once.

Lots
- Every grant is a lot (credit_lots) with its own remaining amount and expiry.
  welcome/adjustment: free credits, no expiry. paid: expires one calendar year (UTC) after
  purchase. legacy: balances from before lots existed; no expiry (terms unchanged).
- Spend order: free lots first, then paid lots by earliest expiry (never-expiring last).
- Available balance = remaining credits in unexpired lots. A lot is expired from expires_at on.
- Reservations record the lots they drew from. A refund returns credits to the same lot unless
  that lot has expired, in which case those credits are forfeited (refunds never revive them).
- Every reserve, refund, grant and expiry runs under a row lock on the customer's
  credit_accounts row, so concurrent requests cannot overspend.

Invariants: credit_accounts.balance == sum(lots.remaining) == sum(ledger.delta) per customer.

BILLING_MODE: off (default; /v1/execute charges nothing) | on.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional

from app import db
from app.accounts import KeyOwner

log = logging.getLogger("uvicorn.error")

INSTANCE_ID = uuid.uuid4()
HEARTBEAT_SECONDS = 15
STALE_INSTANCE_SECONDS = 60
GRANT_SECRET_ENV = "CREDITS_GRANT_SECRET"
PAYMENT_ID_RE = re.compile(r"^[\x21-\x7e]{1,255}$")   # printable ASCII, no spaces (Stripe IDs fit)
MAX_GRANT = 1_000_000_000
MAX_CLOCK_SKEW = timedelta(minutes=5)                 # purchase timestamps may not be in the future
FREE_SOURCES = ("welcome", "adjustment")

# Test hook: when set, returns the "current" time used for expiry decisions instead of now().
_clock: Optional[Callable[[], datetime]] = None

# Free lots first; then earliest expiry (never-expiring last); then oldest lot.
SPEND_ORDER = ("CASE WHEN source IN ('welcome', 'adjustment') THEN 0 ELSE 1 END, "
               "expires_at ASC NULLS LAST, id ASC")
UNEXPIRED = "(expires_at IS NULL OR expires_at > %(now)s)"


def mode() -> str:
    value = (os.environ.get("BILLING_MODE") or "off").strip().lower()
    return value if value in ("off", "on") else "off"


def enforced() -> bool:
    return mode() == "on"


class BillingError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status, self.code, self.extra = status, code, extra


def _now(conn) -> datetime:
    return _clock() if _clock is not None else conn.execute("SELECT now()").fetchone()[0]


# -- accounts, ledger, lots ------------------------------------------------------------------
def _ensure_account(conn, customer_id: str) -> None:
    conn.execute("INSERT INTO customers (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (customer_id,))
    conn.execute("INSERT INTO credit_accounts (customer_id) VALUES (%s) ON CONFLICT (customer_id) DO NOTHING",
                 (customer_id,))


def _lock_account(conn, customer_id: str) -> None:
    """The per-customer mutex: every balance or lot change happens while holding it."""
    _ensure_account(conn, customer_id)
    conn.execute("SELECT 1 FROM credit_accounts WHERE customer_id = %s FOR UPDATE", (customer_id,))


def _ledger(conn, customer_id: str, delta: int, kind: str, *, reservation_id=None, payment_id=None,
            lot_id=None, note: Optional[str] = None) -> int:
    """Change the stored balance and write its ledger row. Caller holds the account lock."""
    row = conn.execute("UPDATE credit_accounts SET balance = balance + %s, updated_at = now() "
                       "WHERE customer_id = %s RETURNING balance", (delta, customer_id)).fetchone()
    conn.execute("INSERT INTO credit_ledger (customer_id, delta, kind, reservation_id, payment_id, lot_id, note, "
                 "balance_after) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                 (customer_id, delta, kind, reservation_id, payment_id, lot_id, note, row[0]))
    return row[0]


def _expire_due(conn, customer_id: str, now: datetime) -> int:
    """Write off unspent credits in lots that have expired. Caller holds the account lock."""
    lots = conn.execute("SELECT id, remaining FROM credit_lots WHERE customer_id = %s AND remaining > 0 "
                        "AND expires_at IS NOT NULL AND expires_at <= %s ORDER BY id FOR UPDATE",
                        (customer_id, now)).fetchall()
    total = 0
    for lot_id, remaining in lots:
        conn.execute("UPDATE credit_lots SET remaining = 0, expired_at = %s WHERE id = %s", (now, lot_id))
        _ledger(conn, customer_id, -remaining, "expire", lot_id=lot_id)
        total += remaining
    return total


def _available(conn, customer_id: str, now: datetime) -> int:
    return int(conn.execute(f"SELECT COALESCE(sum(remaining), 0) FROM credit_lots WHERE customer_id = %(c)s "
                            f"AND remaining > 0 AND {UNEXPIRED}", {"c": customer_id, "now": now}).fetchone()[0])


def _take(conn, customer_id: str, amount: int, now: datetime) -> List[tuple]:
    """Remove `amount` from unexpired lots in spend order. Returns [(lot_id, taken), ...]."""
    lots = conn.execute(f"SELECT id, remaining FROM credit_lots WHERE customer_id = %(c)s AND remaining > 0 "
                        f"AND {UNEXPIRED} ORDER BY {SPEND_ORDER} FOR UPDATE",
                        {"c": customer_id, "now": now}).fetchall()
    taken, need = [], amount
    for lot_id, remaining in lots:
        if need == 0:
            break
        n = min(need, remaining)
        conn.execute("UPDATE credit_lots SET remaining = remaining - %s WHERE id = %s", (n, lot_id))
        taken.append((lot_id, n))
        need -= n
    assert need == 0, "caller must check availability first"
    return taken


def _add_lot(conn, customer_id: str, source: str, credits: int, *, payment_id=None, granted_at=None,
             expires_at=None) -> int:
    return conn.execute("INSERT INTO credit_lots (customer_id, source, payment_id, granted_at, expires_at, "
                        "original, remaining) VALUES (%s, %s, %s, COALESCE(%s, now()), %s, %s, %s) RETURNING id",
                        (customer_id, source, payment_id, granted_at, expires_at, credits, credits)).fetchone()[0]


# -- balances --------------------------------------------------------------------------------
def balance_summary(customer_id: str, recent: int = 20) -> Dict[str, Any]:
    with db.pool().connection() as conn:
        now = _now(conn)
        p = {"c": customer_id, "now": now}
        free, paid = conn.execute(
            f"SELECT COALESCE(sum(remaining) FILTER (WHERE source IN ('welcome', 'adjustment')), 0), "
            f"       COALESCE(sum(remaining) FILTER (WHERE source NOT IN ('welcome', 'adjustment')), 0) "
            f"FROM credit_lots WHERE customer_id = %(c)s AND remaining > 0 AND {UNEXPIRED}", p).fetchone()
        upcoming = conn.execute(
            "SELECT expires_at, sum(remaining) FROM credit_lots WHERE customer_id = %(c)s AND remaining > 0 "
            "AND expires_at > %(now)s GROUP BY expires_at ORDER BY expires_at LIMIT 5", p).fetchall()
        held = conn.execute("SELECT COALESCE(sum(amount), 0) FROM credit_reservations "
                            "WHERE customer_id = %(c)s AND status = 'held'", p).fetchone()[0]
        ledger = conn.execute("SELECT kind, delta, balance_after, created_at, payment_id "
                              "FROM credit_ledger WHERE customer_id = %(c)s ORDER BY id DESC LIMIT %(n)s",
                              {**p, "n": recent}).fetchall()
        grants = conn.execute("SELECT g.payment_id, g.grant_type, g.credits, g.purchased_at, g.expires_at, g.created_at, "
                              "l.remaining FROM credit_grants g LEFT JOIN credit_lots l ON l.payment_id = g.payment_id "
                              "WHERE g.customer_id = %(c)s ORDER BY g.created_at DESC LIMIT 10", p).fetchall()
    expirations = [{"expires_at": t, "amount": int(a)} for t, a in upcoming]
    return {
        "balance": int(free + paid),              # available now: unexpired credits only
        "free_balance": int(free),                # welcome + support credits, spent first
        "paid_balance": int(paid),
        "next_expiration": expirations[0] if expirations else None,
        "upcoming_expirations": expirations,
        "held": int(held),                        # reserved by requests running right now
        "billing_mode": mode(),
        "as_of": now,
        "recent_ledger": [{"kind": k, "delta": d, "balance_after": b, "created_at": t, "payment_id": pid}
                          for k, d, b, t, pid in ledger],
        "recent_grants": [{"payment_id": pid, "type": gt, "credits": c, "purchased_at": pa, "expires_at": ea,
                           "received_at": ra, "remaining": rem if rem is not None else None}
                          for pid, gt, c, pa, ea, ra, rem in grants],
    }


# -- grants ----------------------------------------------------------------------------------
def grant(customer_id: str, credits: int, payment_id: str, grant_type: str = "paid",
          purchased_at: Optional[datetime] = None) -> Dict[str, Any]:
    """Add one lot for one website payment (or welcome grant), exactly once per payment_id.

    paid: expires one calendar year (UTC) after the verified purchase time. For backward
    compatibility a paid grant may omit purchased_at; Railway then uses the time it received
    the grant and records purchased_at_source = 'received'.
    welcome: free credits, no expiry. A replay returns already_credited; reusing a payment_id
    with a different customer, amount or type is refused (409).
    """
    with db.pool().connection() as conn:
        now = _now(conn)
        source = None
        if grant_type == "paid":
            if purchased_at is None:
                purchased_at, source = now, "received"
            else:
                source = "verified"
                if purchased_at > now + MAX_CLOCK_SKEW:
                    raise BillingError(422, "purchased_at_in_future", "purchased_at is in the future")
        _lock_account(conn, customer_id)
        expires = None
        if grant_type == "paid":  # calendar year in UTC: 2028-02-29 -> 2029-02-28
            expires = conn.execute("SELECT ((%s::timestamptz AT TIME ZONE 'UTC') + interval '1 year') AT TIME ZONE 'UTC'",
                                   (purchased_at,)).fetchone()[0]
        first = conn.execute(
            "INSERT INTO credit_grants (payment_id, customer_id, credits, grant_type, purchased_at, expires_at, "
            "purchased_at_source) VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (payment_id) DO NOTHING "
            "RETURNING payment_id",
            (payment_id, customer_id, credits, grant_type, purchased_at, expires, source)).fetchone()
        if first is None:
            prior = conn.execute("SELECT customer_id, credits, grant_type, purchased_at, expires_at, purchased_at_source "
                                 "FROM credit_grants WHERE payment_id = %s", (payment_id,)).fetchone()
            if prior[:3] != (customer_id, credits, grant_type):
                raise BillingError(409, "payment_id_conflict",
                                   "this payment_id was already granted with a different customer, amount or type")
            _expire_due(conn, customer_id, now)
            conn.commit()
            return {"status": "already_credited", "customer_id": customer_id, "credits": credits,
                    "payment_id": payment_id, "type": grant_type, "purchased_at": prior[3], "expires_at": prior[4],
                    "purchased_at_source": prior[5], "balance": _available(conn, customer_id, now)}
        lot = _add_lot(conn, customer_id, grant_type, credits, payment_id=payment_id,
                       granted_at=purchased_at, expires_at=expires)
        _ledger(conn, customer_id, credits, "grant", payment_id=payment_id, lot_id=lot)
        expired_now = _expire_due(conn, customer_id, now)   # e.g. a purchase reported over a year late
        bal = _available(conn, customer_id, now)
    log.info("granted %d %s credits for a website payment", credits, grant_type)
    return {"status": "credited", "customer_id": customer_id, "credits": credits, "payment_id": payment_id,
            "type": grant_type, "purchased_at": purchased_at, "expires_at": expires,
            "purchased_at_source": source, "balance": bal,
            **({"expired_on_arrival": expired_now} if expired_now else {})}


def adjust(customer_id: str, delta: int, note: str) -> int:
    """Operator adjustment. Positive: a free, non-expiring lot. Negative: removed in spend order."""
    with db.pool().connection() as conn:
        now = _now(conn)
        _lock_account(conn, customer_id)
        _expire_due(conn, customer_id, now)
        if delta > 0:
            lot = _add_lot(conn, customer_id, "adjustment", delta)
            _ledger(conn, customer_id, delta, "adjustment", lot_id=lot, note=note[:200])
        else:
            available = _available(conn, customer_id, now)
            if available + delta < 0:
                conn.commit()  # keep the expiry write-off even though the adjustment is refused
                raise BillingError(409, "balance_would_go_negative", "adjustment would make the balance negative",
                                   balance=available)
            for lot_id, n in _take(conn, customer_id, -delta, now):
                _ledger(conn, customer_id, -n, "adjustment", lot_id=lot_id, note=note[:200])
        return _available(conn, customer_id, now)


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
    """Atomically hold `amount` unexpired credits or raise BillingError(402, insufficient_credits)."""
    rid = uuid.uuid4()
    with db.pool().connection() as conn:
        now = _now(conn)
        # Make sure this process is registered (the maintenance thread keeps its heartbeat fresh).
        conn.execute("INSERT INTO api_instances (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (INSTANCE_ID,))
        _lock_account(conn, owner.customer_id)
        _expire_due(conn, owner.customer_id, now)
        available = _available(conn, owner.customer_id, now)
        if available < amount:
            conn.commit()  # keep the expiry write-off even though the request is refused
            raise BillingError(402, "insufficient_credits",
                               f"this request needs {amount} credit(s) but the available balance is {available}",
                               required=amount, balance=available)
        conn.execute("INSERT INTO credit_reservations (id, customer_id, key_id, instance_id, amount, expires_at) "
                     "VALUES (%s, %s, %s, %s, %s, now() + make_interval(secs => %s))",
                     (rid, owner.customer_id, owner.key_id, INSTANCE_ID, amount, max_seconds))
        for seq, (lot_id, n) in enumerate(_take(conn, owner.customer_id, amount, now)):
            conn.execute("INSERT INTO credit_reservation_lots (reservation_id, seq, lot_id, amount) "
                         "VALUES (%s, %s, %s, %s)", (rid, seq, lot_id, n))
        _ledger(conn, owner.customer_id, -amount, "reserve", reservation_id=rid)
    return Reservation(rid, owner, amount)


def _release(conn, rid, customer_id: str, amount: int, charged: int, status: str, now: datetime) -> int:
    """Keep the first `charged` credits (spend order); return the rest to their lots unless expired.

    Caller holds the reservation row lock and the account lock. Returns credits forfeited.
    """
    refund = amount - charged
    forfeited = 0
    allocations = conn.execute("SELECT seq, lot_id, amount FROM credit_reservation_lots WHERE reservation_id = %s "
                               "ORDER BY seq DESC", (rid,)).fetchall()
    if refund and not allocations:  # held before lots existed (migration 4): refund into a legacy lot
        lot = conn.execute("INSERT INTO credit_lots (customer_id, source, original, remaining) "
                           "VALUES (%s, 'legacy', %s, 0) RETURNING id", (customer_id, refund)).fetchone()[0]
        allocations = [(0, lot, refund)]
    for _seq, lot_id, n in allocations:  # refund the last-drawn credits first
        if refund == 0:
            break
        back = min(refund, n)
        refund -= back
        alive = conn.execute("SELECT expires_at IS NULL OR expires_at > %s FROM credit_lots WHERE id = %s FOR UPDATE",
                             (now, lot_id)).fetchone()[0]
        if not alive:  # expired since it was reserved: these credits are gone
            forfeited += back
            continue
        conn.execute("UPDATE credit_lots SET remaining = remaining + %s WHERE id = %s", (back, lot_id))
        _ledger(conn, customer_id, back, "release", reservation_id=rid, lot_id=lot_id)
    conn.execute("UPDATE credit_reservations SET status = %s, charged = %s, started = %s, forfeited = %s, "
                 "settled_at = now() WHERE id = %s", (status, charged, charged, forfeited, rid))
    return forfeited


def settle(res: Reservation) -> int:
    """Charge started operations, release the rest. Idempotent. Returns credits charged."""
    with db.pool().connection() as conn:
        row = conn.execute("SELECT status, amount, charged FROM credit_reservations WHERE id = %s FOR UPDATE",
                           (res.id,)).fetchone()
        if row[0] != "held":  # already released by the sweeper
            return row[2] or 0
        now = _now(conn)
        _lock_account(conn, res.owner.customer_id)
        charged = min(res.started, row[1])
        _release(conn, res.id, res.owner.customer_id, row[1], charged, "settled", now)
    return charged


# -- maintenance -----------------------------------------------------------------------------
def heartbeat() -> None:
    with db.pool().connection() as conn:
        conn.execute("INSERT INTO api_instances (id) VALUES (%s) ON CONFLICT (id) DO UPDATE SET heartbeat_at = now()",
                     (INSTANCE_ID,))


def sweep() -> int:
    """Release reservations whose API process died (stale heartbeat) or that outlived their expiry.

    Operations recorded as started are charged; everything else is refunded (or forfeited if its
    lot has expired).
    """
    released = 0
    with db.pool().connection() as conn:
        now = _now(conn)
        rows = conn.execute(
            "SELECT r.id, r.customer_id, r.amount, r.started FROM credit_reservations r "
            "JOIN api_instances i ON i.id = r.instance_id "
            "WHERE r.status = 'held' AND (i.heartbeat_at < now() - make_interval(secs => %s) OR r.expires_at < now()) "
            "ORDER BY r.created_at LIMIT 200 FOR UPDATE OF r SKIP LOCKED",
            (STALE_INSTANCE_SECONDS,)).fetchall()
        for rid, customer_id, amount, started in rows:
            _lock_account(conn, customer_id)
            _release(conn, rid, customer_id, amount, started, "released", now)
            released += 1
    if released:
        log.warning("released %d orphaned credit reservation(s)", released)
    return released


def expire_sweep() -> int:
    """Write off credits in lots that have expired, so balances and ledgers reflect it."""
    total = 0
    with db.pool().connection() as conn:
        now = _now(conn)
        customers = [r[0] for r in conn.execute(
            "SELECT DISTINCT customer_id FROM credit_lots WHERE remaining > 0 AND expires_at IS NOT NULL "
            "AND expires_at <= %s LIMIT 500", (now,)).fetchall()]
    for customer_id in customers:
        with db.pool().connection() as conn:
            _lock_account(conn, customer_id)
            total += _expire_due(conn, customer_id, _now(conn))
    if total:
        log.info("expired %d credit(s) across %d customer(s)", total, len(customers))
    return total


class Maintenance(threading.Thread):
    """Heartbeat, orphaned-reservation release and expiry write-offs for this API process."""

    def __init__(self):
        super().__init__(daemon=True, name="billing-maintenance")
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                heartbeat()
                sweep()
                expire_sweep()
            except Exception as exc:
                log.warning("billing maintenance failed: %s", type(exc).__name__)
            self.stop.wait(HEARTBEAT_SECONDS)


# -- report on pre-expiration balances --------------------------------------------------------
def expiry_report() -> Dict[str, Any]:
    """Read-only: how legacy (pre-expiration) balances would be affected by the one-year rule.

    Nothing is changed. Legacy credits currently never expire. This shows what would happen if
    each legacy grant were given an expiry of one year after Railway received it (no verified
    purchase timestamp exists for them), attributing each customer's remaining legacy balance to
    their most recent inflows first (oldest credits are assumed spent first, matching the new
    earliest-expiration spend order). Positive operator adjustments are treated as free credits.
    """
    with db.pool().connection() as conn:
        now = _now(conn)
        legacy = conn.execute("SELECT customer_id, sum(remaining), sum(original) FROM credit_lots "
                              "WHERE source = 'legacy' GROUP BY customer_id ORDER BY customer_id").fetchall()
        customers, totals = [], {"legacy_remaining": 0, "would_expire_immediately": 0,
                                 "would_expire_later": 0, "would_never_expire": 0}
        for customer_id, remaining, _original in legacy:
            inflows = conn.execute(
                "SELECT 'grant', g.payment_id, g.credits, g.created_at, "
                "       ((g.created_at AT TIME ZONE 'UTC') + interval '1 year') AT TIME ZONE 'UTC' "
                "FROM credit_grants g WHERE g.customer_id = %(c)s "
                "AND NOT EXISTS (SELECT 1 FROM credit_lots l WHERE l.payment_id = g.payment_id) "
                "UNION ALL SELECT 'adjustment', NULL, delta, created_at, NULL FROM credit_ledger "
                "WHERE customer_id = %(c)s AND kind = 'adjustment' AND delta > 0 AND lot_id IS NULL "
                "ORDER BY 4 DESC", {"c": customer_id}).fetchall()
            left, rows = int(remaining), []
            for kind, payment_id, credits, received, expires in inflows:
                attributed = min(left, int(credits))
                left -= attributed
                outcome = ("free_no_expiry" if kind == "adjustment" else
                           "would_expire_immediately" if expires <= now else "would_expire_later")
                if attributed == 0:
                    outcome = "already_spent"
                rows.append({"source": kind, "payment_id": payment_id, "credits": int(credits),
                             "received_at": received, "attributed_remaining": attributed,
                             "proposed_expires_at": expires, "outcome": outcome})
                if attributed:
                    key = {"free_no_expiry": "would_never_expire"}.get(outcome, outcome)
                    totals[key] += attributed
            if left:  # balance with no recorded inflow (should not happen): keep it non-expiring
                rows.append({"source": "unattributed", "payment_id": None, "credits": left, "received_at": None,
                             "attributed_remaining": left, "proposed_expires_at": None,
                             "outcome": "would_never_expire"})
                totals["would_never_expire"] += left
            totals["legacy_remaining"] += int(remaining)
            customers.append({"customer_id": customer_id, "legacy_remaining": int(remaining), "inflows": rows})
    return {
        "as_of": now,
        "current_handling": "legacy credits keep their original terms: they never expire",
        "proposal_evaluated": "expire each legacy paid grant one year after Railway received it",
        "totals": totals,
        "customers": customers,
    }
