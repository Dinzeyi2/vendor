"""Prepaid credits: balances, reservations, Stripe Checkout purchases and webhook crediting.

Money rules
- One balance per customer (credit_accounts), shared by all its keys, never negative.
- Every change is a credit_ledger row; balance == sum(delta).
- /v1/execute reserves the whole batch cost in one transaction (row lock on the balance),
  then settles to the number of operations that actually started; the rest is released.
- Reservations of a crashed/restarted API process are released by the sweeper, charging
  only operations recorded as started.
- A purchase is credited once: the webhook records the Stripe event, locks the purchase row
  and inserts a ledger row protected by a unique index, all in one transaction.

BILLING_MODE: off (default; nothing is charged, checkout disabled) | test | live.
test requires Stripe test keys and live requires live keys, so live billing cannot be
switched on by accident.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import stripe

from app import db
from app.accounts import KeyOwner

log = logging.getLogger("uvicorn.error")

INSTANCE_ID = uuid.uuid4()
HEARTBEAT_SECONDS = 15
STALE_INSTANCE_SECONDS = 60
PACK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


# -- configuration ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Pack:
    id: str
    price_id: str
    credits: int


@dataclass
class BillingConfig:
    mode: str
    secret_key: str = field(repr=False, default="")
    webhook_secret: str = field(repr=False, default="")
    packs: Dict[str, Pack] = field(default_factory=dict)
    success_url: str = ""
    cancel_url: str = ""
    problems: List[str] = field(default_factory=list)

    @property
    def enforced(self) -> bool:
        return self.mode in ("test", "live")

    @property
    def livemode(self) -> bool:
        return self.mode == "live"


def config() -> BillingConfig:
    mode = (os.environ.get("BILLING_MODE") or "off").strip().lower()
    cfg = BillingConfig(mode=mode if mode in ("off", "test", "live") else "off",
                        secret_key=os.environ.get("STRIPE_SECRET_KEY", "").strip(),
                        webhook_secret=os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip(),
                        success_url=os.environ.get("CHECKOUT_SUCCESS_URL", "").strip(),
                        cancel_url=os.environ.get("CHECKOUT_CANCEL_URL", "").strip())
    if mode not in ("off", "test", "live"):
        cfg.problems.append("BILLING_MODE must be off, test or live (treated as off)")
    if cfg.mode == "off":
        return cfg
    want = "live" if cfg.livemode else "test"
    if not re.match(rf"^(sk|rk)_{want}_", cfg.secret_key):
        cfg.problems.append(f"STRIPE_SECRET_KEY must be a Stripe {want}-mode secret key in BILLING_MODE={cfg.mode}")
    if not cfg.webhook_secret.startswith("whsec_"):
        cfg.problems.append("STRIPE_WEBHOOK_SECRET must be set (whsec_...)")
    for name, url in (("CHECKOUT_SUCCESS_URL", cfg.success_url), ("CHECKOUT_CANCEL_URL", cfg.cancel_url)):
        if not url.startswith("https://") and not (cfg.mode == "test" and url.startswith("http://")):
            cfg.problems.append(f"{name} must be an https:// URL")
    try:
        raw = json.loads(os.environ.get("CREDIT_PACKS") or "{}")
        if not isinstance(raw, dict):
            raise ValueError
        for pid, spec in raw.items():
            credits, price = spec.get("credits"), spec.get("price_id")
            if (not PACK_ID_RE.fullmatch(str(pid)) or isinstance(credits, bool) or not isinstance(credits, int)
                    or not 0 < credits <= 100_000_000 or not str(price).startswith("price_")):
                raise ValueError(pid)
            cfg.packs[pid] = Pack(pid, price, credits)
    except (ValueError, AttributeError, TypeError):
        cfg.problems.append('CREDIT_PACKS must be JSON like {"starter": {"price_id": "price_...", "credits": 1000}}')
    if not cfg.packs:
        cfg.problems.append("CREDIT_PACKS defines no packs")
    return cfg


class BillingError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra: Any):
        super().__init__(message)
        self.status, self.code, self.extra = status, code, extra


# -- balances & ledger -----------------------------------------------------------------------
def _ensure_account(conn, customer_id: str) -> None:
    conn.execute("INSERT INTO customers (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (customer_id,))
    conn.execute("INSERT INTO credit_accounts (customer_id) VALUES (%s) ON CONFLICT (customer_id) DO NOTHING",
                 (customer_id,))


def _apply(conn, customer_id: str, delta: int, kind: str, *, reservation_id=None, purchase_id=None,
           note: Optional[str] = None) -> int:
    """Change a balance and write its ledger row. Caller holds the transaction."""
    row = conn.execute("UPDATE credit_accounts SET balance = balance + %s, updated_at = now() "
                       "WHERE customer_id = %s RETURNING balance", (delta, customer_id)).fetchone()
    conn.execute("INSERT INTO credit_ledger (customer_id, delta, kind, reservation_id, purchase_id, note, balance_after) "
                 "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                 (customer_id, delta, kind, reservation_id, purchase_id, note, row[0]))
    return row[0]


def balance_summary(customer_id: str, recent: int = 20) -> Dict[str, Any]:
    with db.pool().connection() as conn:
        row = conn.execute("SELECT balance FROM credit_accounts WHERE customer_id = %s", (customer_id,)).fetchone()
        held = conn.execute("SELECT COALESCE(sum(amount), 0) FROM credit_reservations "
                            "WHERE customer_id = %s AND status = 'held'", (customer_id,)).fetchone()[0]
        ledger = conn.execute("SELECT kind, delta, balance_after, created_at, purchase_id, reservation_id "
                              "FROM credit_ledger WHERE customer_id = %s ORDER BY id DESC LIMIT %s",
                              (customer_id, recent)).fetchall()
        purchases = conn.execute("SELECT id, pack_id, credits, status, amount_total, currency, created_at, paid_at "
                                 "FROM credit_purchases WHERE customer_id = %s ORDER BY created_at DESC LIMIT 10",
                                 (customer_id,)).fetchall()
    cfg = config()
    return {
        "balance": row[0] if row else 0,
        "held": int(held),  # reserved by requests currently running (already deducted from balance)
        "billing_mode": cfg.mode,
        "packs": [{"id": p.id, "credits": p.credits} for p in cfg.packs.values()],
        "recent_ledger": [{"kind": k, "delta": d, "balance_after": b, "created_at": t,
                           "purchase_id": str(p) if p else None, "reservation_id": str(r) if r else None}
                          for k, d, b, t, p, r in ledger],
        "recent_purchases": [{"id": str(i), "pack_id": pk, "credits": c, "status": s, "amount_total": a,
                              "currency": cur, "created_at": t, "paid_at": pa}
                             for i, pk, c, s, a, cur, t, pa in purchases],
    }


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
def register_instance() -> None:
    with db.pool().connection() as conn:
        conn.execute("INSERT INTO api_instances (id) VALUES (%s) ON CONFLICT (id) DO UPDATE SET heartbeat_at = now()",
                     (INSTANCE_ID,))


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


# -- Stripe Checkout -------------------------------------------------------------------------
def _ready(cfg: BillingConfig) -> None:
    if not cfg.enforced:
        raise BillingError(503, "billing_disabled", "billing is disabled on this server (BILLING_MODE=off)")
    if cfg.problems:
        log.error("billing misconfigured: %s", "; ".join(cfg.problems))
        raise BillingError(503, "billing_misconfigured", "billing is not configured correctly on the server")


def create_checkout(customer_id: str, pack_id: str) -> Dict[str, Any]:
    cfg = config()
    _ready(cfg)
    pack = cfg.packs.get(pack_id)
    if pack is None:
        raise BillingError(404, "unknown_pack", f"unknown pack '{pack_id}'", packs=sorted(cfg.packs))
    purchase_id = uuid.uuid4()
    with db.pool().connection() as conn:
        _ensure_account(conn, customer_id)
        conn.execute("INSERT INTO credit_purchases (id, customer_id, pack_id, credits, stripe_price_id, livemode) "
                     "VALUES (%s, %s, %s, %s, %s, %s)",
                     (purchase_id, customer_id, pack.id, pack.credits, pack.price_id, cfg.livemode))
    meta = {"purchase_id": str(purchase_id), "pack_id": pack.id}
    try:
        session = stripe.checkout.Session.create(
            api_key=cfg.secret_key, idempotency_key=f"checkout-{purchase_id}",
            mode="payment", line_items=[{"price": pack.price_id, "quantity": 1}],
            client_reference_id=customer_id, metadata=meta, payment_intent_data={"metadata": meta},
            success_url=cfg.success_url, cancel_url=cfg.cancel_url)
    except stripe.StripeError as exc:
        with db.pool().connection() as conn:
            conn.execute("UPDATE credit_purchases SET status = 'failed' WHERE id = %s", (purchase_id,))
        log.error("stripe checkout creation failed: %s (code %s)", type(exc).__name__, getattr(exc, "code", None))
        raise BillingError(502, "checkout_failed", "could not create the checkout session") from None
    with db.pool().connection() as conn:
        conn.execute("UPDATE credit_purchases SET stripe_session_id = %s WHERE id = %s", (session.id, purchase_id))
    return {"purchase_id": str(purchase_id), "pack_id": pack.id, "credits": pack.credits,
            "checkout_session_id": session.id, "checkout_url": session.url}


# -- Stripe webhook --------------------------------------------------------------------------
PAID_EVENTS = {"checkout.session.completed", "checkout.session.async_payment_succeeded"}
NO_PAY_EVENTS = {"checkout.session.expired": "expired", "checkout.session.async_payment_failed": "failed"}


def handle_webhook(payload: bytes, signature: Optional[str]) -> Dict[str, Any]:
    """Verify a Stripe webhook against the raw body and apply it exactly once."""
    cfg = config()
    if not cfg.enforced or not cfg.webhook_secret.startswith("whsec_"):
        raise BillingError(503, "billing_disabled", "webhooks are not accepted while billing is disabled")
    try:
        stripe.Webhook.construct_event(payload, signature or "", cfg.webhook_secret)  # verifies or raises
        event = json.loads(payload)  # the verified raw body, as plain dicts
    except (stripe.SignatureVerificationError, ValueError):
        raise BillingError(400, "invalid_signature", "webhook signature verification failed") from None

    etype, livemode = event["type"], bool(event["livemode"])
    if livemode != cfg.livemode:
        log.warning("ignored stripe event %s: livemode=%s in BILLING_MODE=%s", event["id"], livemode, cfg.mode)
        return {"result": "ignored_mode_mismatch"}
    if etype not in PAID_EVENTS and etype not in NO_PAY_EVENTS:
        return {"result": "ignored_event_type"}

    session = event["data"]["object"]
    with db.pool().connection() as conn:
        first = conn.execute("INSERT INTO stripe_events (id, type, livemode, result) VALUES (%s, %s, %s, 'processing') "
                             "ON CONFLICT (id) DO NOTHING RETURNING id", (event["id"], etype, livemode)).fetchone()
        if first is None:
            return {"result": "duplicate_event"}
        result = _apply_session_event(conn, etype, session)
        conn.execute("UPDATE stripe_events SET result = %s WHERE id = %s", (result, event["id"]))
    log.info("stripe event %s (%s): %s", event["id"], etype, result)
    return {"result": result}


def _find_purchase(conn, session) -> Optional[tuple]:
    cols = "id, customer_id, credits, status, livemode, stripe_session_id"
    row = conn.execute(f"SELECT {cols} FROM credit_purchases WHERE stripe_session_id = %s FOR UPDATE",
                       (session["id"],)).fetchone()
    if row is None:
        # The session-id update can be lost if the API died right after Stripe created the session.
        # metadata and client_reference_id were set by this server, and the event is Stripe-signed.
        pid = (session.get("metadata") or {}).get("purchase_id")
        try:
            pid = uuid.UUID(pid) if pid else None
        except ValueError:
            pid = None
        if pid:
            row = conn.execute(f"SELECT {cols} FROM credit_purchases WHERE id = %s AND customer_id = %s "
                               f"AND stripe_session_id IS NULL FOR UPDATE",
                               (pid, session.get("client_reference_id"))).fetchone()
            if row:
                conn.execute("UPDATE credit_purchases SET stripe_session_id = %s WHERE id = %s", (session["id"], pid))
    return row


def _apply_session_event(conn, etype: str, session) -> str:
    purchase = _find_purchase(conn, session)
    if purchase is None:
        return "unknown_session"
    pid, customer_id, credits, status, livemode, _ = purchase
    if etype in NO_PAY_EVENTS:
        if status == "pending":
            conn.execute("UPDATE credit_purchases SET status = %s WHERE id = %s", (NO_PAY_EVENTS[etype], pid))
        return f"no_credit_{NO_PAY_EVENTS[etype]}"
    # A paid event: credit only once, and only for a confirmed payment.
    # "unpaid" (async methods still pending) and "no_payment_required" (no money received) add nothing.
    if session.get("payment_status") != "paid" or session.get("mode") != "payment":
        return "not_paid"
    if status == "paid":
        return "already_credited"
    if livemode != bool(session.get("livemode")):
        return "mode_mismatch"
    conn.execute("UPDATE credit_purchases SET status = 'paid', paid_at = now(), amount_total = %s, currency = %s "
                 "WHERE id = %s", (session.get("amount_total"), session.get("currency"), pid))
    _apply(conn, customer_id, credits, "purchase", purchase_id=pid)
    return "credited"
