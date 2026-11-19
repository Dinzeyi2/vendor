"""Prepaid credits against a real local Postgres (pgserver).

Stripe: webhook events are signed with the same HMAC-SHA256 scheme Stripe uses and verified by
the real stripe library (construct_event). Only Checkout session *creation* is mocked here;
real Stripe test mode is exercised by scripts/billing_smoke_test.py.
"""
import hashlib
import hmac
import json
import secrets
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
import stripe
from fastapi.testclient import TestClient

from app import billing, db
from app.accounts import KeyOwner
from app.main import app
from app.registry import OPERATIONS, WORKERS, OpSpec
from app.workers import WorkerFailure, WorkerProcess

pgserver = pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

SERVER_SECRET = "test-website-backend-secret-" + "y" * 16
OPERATOR_KEY = "operator-test-key"
WEBHOOK_SECRET = "whsec_test_" + "z" * 24
PACKS = {"starter": {"price_id": "price_test_starter", "credits": 5},
         "big": {"price_id": "price_test_big", "credits": 100}}
client = TestClient(app)


@pytest.fixture(scope="module")
def database_url():
    data = Path(__file__).resolve().parent.parent / ".runtimes" / "pgdata"
    srv = pgserver.get_server(str(data), cleanup_mode=None)
    name = "b_" + uuid.uuid4().hex[:12]
    with psycopg.connect(srv.get_uri(), autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name}")
    yield srv.get_uri().rsplit("/", 1)[0] + "/" + name
    db.close()
    with psycopg.connect(srv.get_uri(), autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture(autouse=True)
def env(monkeypatch, database_url):
    db.close()  # each test sees this module's database
    for k, v in {"DATABASE_URL": database_url, "WEBSITE_BACKEND_SECRET": SERVER_SECRET, "API_KEY": OPERATOR_KEY,
                 "BILLING_MODE": "test", "STRIPE_SECRET_KEY": "sk_test_dummy", "STRIPE_WEBHOOK_SECRET": WEBHOOK_SECRET,
                 "CREDIT_PACKS": json.dumps(PACKS), "CHECKOUT_SUCCESS_URL": "https://example.com/ok",
                 "CHECKOUT_CANCEL_URL": "https://example.com/cancel"}.items():
        monkeypatch.setenv(k, v)
    yield
    db.close()


@pytest.fixture
def stripe_sessions(monkeypatch):
    """Mock Checkout session creation; record what the server asked Stripe for."""
    calls = []

    def create(**kw):
        calls.append(kw)
        sid = "cs_test_" + secrets.token_hex(12)
        return SimpleNamespace(id=sid, url=f"https://checkout.stripe.com/c/pay/{sid}")
    monkeypatch.setattr(stripe.checkout.Session, "create", staticmethod(create))
    return calls


# -- helpers -----------------------------------------------------------------------------------
def site(customer):
    return {"Authorization": f"Bearer {SERVER_SECRET}", "X-Customer-Id": customer}


def new_customer():
    return "cust_" + secrets.token_hex(6)


def new_key(customer):
    r = client.post("/v1/account/keys", headers=site(customer), json={"name": "k"})
    assert r.status_code == 201
    return r.json()


def balance(customer):
    r = client.get("/v1/account/credits", headers=site(customer))
    assert r.status_code == 200
    return r.json()["balance"]


def fuzz(n):
    return [{"id": f"t{i}", "operation": "rapidfuzz.compare", "inputs": {"a": "x", "b": "y"}} for i in range(n)]


def execute(key, tasks):
    return client.post("/v1/execute", headers={"X-API-Key": key}, json={"tasks": tasks})


def sign(payload: bytes, secret=WEBHOOK_SECRET, t=None) -> str:
    t = t or int(time.time())
    mac = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def session_event(etype, session_id, *, payment_status="paid", livemode=False, event_id=None, metadata=None,
                  customer=None):
    return json.dumps({
        "id": event_id or "evt_" + secrets.token_hex(12), "object": "event", "type": etype, "livemode": livemode,
        "created": int(time.time()), "api_version": "2024-06-20",
        "data": {"object": {"id": session_id, "object": "checkout.session", "mode": "payment",
                            "payment_status": payment_status, "livemode": livemode, "amount_total": 500,
                            "currency": "usd", "client_reference_id": customer, "metadata": metadata or {}}},
    }).encode()


def deliver(payload, signature=None):
    return client.post("/v1/stripe/webhook", content=payload,
                       headers={"Stripe-Signature": signature or sign(payload), "Content-Type": "application/json"})


def checkout(customer, pack="starter"):
    r = client.post("/v1/account/credits/checkout", headers=site(customer), json={"pack_id": pack})
    assert r.status_code == 201, r.text
    return r.json()


def buy(customer, pack="starter"):
    co = checkout(customer, pack)
    r = deliver(session_event("checkout.session.completed", co["checkout_session_id"]))
    assert r.status_code == 200 and r.json()["result"] == "credited", r.text
    return co


def assert_ledger_consistent(database_url):
    with psycopg.connect(database_url) as conn:
        bad = conn.execute(
            "SELECT a.customer_id, a.balance, COALESCE(sum(l.delta), 0) FROM credit_accounts a "
            "LEFT JOIN credit_ledger l ON l.customer_id = a.customer_id "
            "GROUP BY a.customer_id, a.balance HAVING a.balance <> COALESCE(sum(l.delta), 0)").fetchall()
        held = conn.execute("SELECT count(*) FROM credit_reservations WHERE status = 'held'").fetchone()[0]
    assert bad == [], f"balance != sum(ledger) for {bad}"
    assert held == 0, "reservations left held"


# -- balances & keys ---------------------------------------------------------------------------
def test_new_account_starts_at_zero_and_key_changes_never_touch_balance(stripe_sessions, database_url):
    c = new_customer()
    assert balance(c) == 0
    k1 = new_key(c)
    assert balance(c) == 0
    buy(c)
    k2 = new_key(c)
    client.post(f"/v1/account/keys/{k1['id']}/revoke", headers=site(c))
    client.post(f"/v1/account/keys/{k2['id']}/revoke", headers=site(c))
    new_key(c)
    assert balance(c) == 5
    assert_ledger_consistent(database_url)


def test_multiple_keys_share_one_balance(stripe_sessions, database_url):
    c = new_customer()
    k1, k2 = new_key(c)["secret"], new_key(c)["secret"]
    buy(c)                                              # 5 credits
    assert execute(k1, fuzz(2)).status_code == 200      # 3 left
    assert execute(k2, fuzz(3)).status_code == 200      # 0 left
    assert balance(c) == 0
    for k in (k1, k2):
        r = execute(k, fuzz(1))
        assert r.status_code == 402 and r.json()["error"] == "insufficient_credits"
    assert_ledger_consistent(database_url)


def test_two_customers_stay_isolated(stripe_sessions, database_url):
    a, b = new_customer(), new_customer()
    ka, kb = new_key(a)["secret"], new_key(b)["secret"]
    buy(a)
    assert (balance(a), balance(b)) == (5, 0)
    assert execute(kb, fuzz(1)).status_code == 402      # A's credits never pay for B
    assert execute(ka, fuzz(1)).status_code == 200
    assert (balance(a), balance(b)) == (4, 0)
    credits_b = client.get("/v1/account/credits", headers=site(b)).json()
    assert credits_b["recent_purchases"] == [] and credits_b["recent_ledger"] == []
    assert_ledger_consistent(database_url)


# -- charging rules ----------------------------------------------------------------------------
def test_insufficient_credits_blocks_and_runs_nothing(stripe_sessions, monkeypatch, database_url):
    ran = []
    spec = OPERATIONS["rapidfuzz.compare"]
    monkeypatch.setitem(OPERATIONS, "rapidfuzz.compare",
                        OpSpec(spec.library, spec.default_timeout, spec.max_timeout,
                               inline=lambda inputs: ran.append(1) or spec.inline(inputs)))
    c = new_customer()
    k = new_key(c)["secret"]
    r = execute(k, fuzz(1))                              # balance 0
    assert r.status_code == 402
    assert r.json() == {"error": "insufficient_credits", "message": r.json()["message"], "required": 1, "balance": 0}
    buy(c)                                               # 5 credits
    r = execute(k, fuzz(6))                              # batch needs 6
    assert r.status_code == 402 and r.json()["required"] == 6 and r.json()["balance"] == 5
    assert ran == [] and balance(c) == 5                 # nothing ran, nothing charged
    usage = client.get("/v1/account/usage", headers=site(c)).json()
    assert usage["totals"]["requests"] == 0
    assert execute(k, fuzz(5)).status_code == 200 and len(ran) == 5 and balance(c) == 0
    assert_ledger_consistent(database_url)


def test_unknown_operations_are_rejected_free(stripe_sessions, database_url):
    c = new_customer()
    k = new_key(c)["secret"]
    r = execute(k, [{"id": "x", "operation": "nope.op", "inputs": {}}])
    assert r.status_code == 200 and r.json()["results"][0]["error_type"] == "unknown_operation"
    buy(c)
    r = execute(k, fuzz(1) + [{"id": "x", "operation": "nope.op", "inputs": {}}])
    assert r.status_code == 200 and balance(c) == 4      # only the known operation was charged
    u = client.get("/v1/account/usage", headers=site(c)).json()
    assert u["totals"]["credits_charged"] == 1
    assert_ledger_consistent(database_url)


def test_started_failures_are_charged_and_unstarted_work_is_refunded(stripe_sessions, monkeypatch, database_url):
    def fail_after_start(op, inputs, timeout, on_start=None):
        on_start()
        raise WorkerFailure("worker_crashed", "worker 'scs' exited (code 1) while running scs.solve", started=True)

    def fail_before_start(op, inputs, timeout, on_start=None):
        raise WorkerFailure("timeout", "worker 'xgboost' stayed busy for 1s", started=False)

    monkeypatch.setattr(WORKERS["scs"], "call", fail_after_start)
    monkeypatch.setattr(WORKERS["xgboost"], "call", fail_before_start)
    c = new_customer()
    k = new_key(c)["secret"]
    buy(c)                                               # 5
    r = execute(k, [{"id": "a", "operation": "scs.solve", "inputs": {}},
                    {"id": "b", "operation": "xgboost.train_predict", "inputs": {}},
                    *fuzz(1)])
    assert r.status_code == 200
    assert [t["status"] for t in r.json()["results"]] == ["error", "error", "ok"]
    assert balance(c) == 3                               # scs (started, failed) + fuzz charged; xgboost refunded
    assert_ledger_consistent(database_url)


def test_real_worker_crash_and_restart(stripe_sessions, monkeypatch, database_url):
    """A worker that crashes mid-run is charged; a worker that cannot start is refunded."""
    py = sys.executable
    good = WorkerProcess("testw", [py, "-m", "app.worker_main", "tests.fake_worker_ops"])
    dead = WorkerProcess("deadw", [py, "-c", "import sys; sys.exit(3)"])
    monkeypatch.setitem(WORKERS, "testw", good)
    monkeypatch.setitem(WORKERS, "deadw", dead)
    for name, worker in (("test.crash", "testw"), ("test.echo", "testw"), ("test.dead", "deadw")):
        monkeypatch.setitem(OPERATIONS, name, OpSpec("Test", 30, 30, worker=worker))
    try:
        c = new_customer()
        k = new_key(c)["secret"]
        buy(c)                                           # 5
        r = execute(k, [{"id": "crash", "operation": "test.crash", "inputs": {}}])
        assert r.json()["results"][0]["error_type"] == "worker_crashed"
        assert balance(c) == 4                           # it started, so it is charged
        r = execute(k, [{"id": "after", "operation": "test.echo", "inputs": {"x": 1}}])
        assert r.json()["results"][0]["status"] == "ok"  # worker restarted on the next call
        assert balance(c) == 3
        r = execute(k, [{"id": "never", "operation": "test.dead", "inputs": {}}])
        assert r.json()["results"][0]["error_type"] == "worker_crashed"
        assert balance(c) == 3                           # never started: refunded
    finally:
        good.kill()
        dead.kill()
    assert_ledger_consistent(database_url)


def test_busy_worker_timeout_does_not_count_as_started():
    w = WorkerProcess("busyw", [sys.executable, "-m", "app.worker_main", "tests.fake_worker_ops"])
    starts = []
    try:
        t = threading.Thread(target=w.call, args=("sleep", {"seconds": 3}, 30))
        t.start()
        time.sleep(1.5)  # the sleep call holds the worker
        with pytest.raises(WorkerFailure) as e:
            w.call("echo", {}, 0.5, on_start=lambda: starts.append(1))
        assert e.value.error_type == "timeout" and e.value.started is False and starts == []
        t.join()
    finally:
        w.kill()


def test_operator_key_is_never_charged(stripe_sessions):
    r = execute(OPERATOR_KEY, fuzz(3))
    assert r.status_code == 200 and all(t["status"] == "ok" for t in r.json()["results"])


# -- concurrency -------------------------------------------------------------------------------
def test_concurrent_reservations_cannot_overspend(stripe_sessions, database_url):
    c = new_customer()
    key = new_key(c)
    buy(c, "starter")                                    # 5
    buy(c, "starter")                                    # 10
    owner = KeyOwner(c, uuid.UUID(key["id"]))
    barrier = threading.Barrier(40)

    def attempt(_):
        barrier.wait()
        try:
            res = billing.reserve(owner, 3, 60)
        except billing.BillingError as exc:
            assert exc.code == "insufficient_credits"
            return 0
        res.started = 3
        billing.settle(res)
        return 3

    with ThreadPoolExecutor(40) as pool:
        spent = sum(pool.map(attempt, range(40)))
    assert spent == 9 and balance(c) == 1                # 3 of 40 succeed; never below zero
    assert_ledger_consistent(database_url)


def test_concurrent_http_requests_cannot_overspend(stripe_sessions, database_url):
    c = new_customer()
    k1, k2 = new_key(c)["secret"], new_key(c)["secret"]
    buy(c)                                               # 5
    with TestClient(app) as tc, ThreadPoolExecutor(20) as pool:
        codes = list(pool.map(lambda i: tc.post("/v1/execute", headers={"X-API-Key": (k1, k2)[i % 2]},
                                                json={"tasks": fuzz(2)}).status_code, range(20)))
    assert codes.count(200) == 2 and codes.count(402) == 18   # 2 x 2 credits fit in 5
    assert balance(c) == 1
    assert_ledger_consistent(database_url)


# -- crash recovery ----------------------------------------------------------------------------
def test_sweeper_releases_reservations_of_dead_process(stripe_sessions, database_url):
    c = new_customer()
    key = new_key(c)
    buy(c)                                               # 5
    res = billing.reserve(KeyOwner(c, uuid.UUID(key["id"])), 4, 3600)
    res.mark_started()                                   # 1 of 4 started before the "crash"
    assert balance(c) == 1
    with psycopg.connect(database_url, autocommit=True) as conn:  # simulate a dead API process
        conn.execute("UPDATE api_instances SET heartbeat_at = now() - interval '10 minutes' WHERE id = %s",
                     (billing.INSTANCE_ID,))
    assert billing.sweep() == 1
    assert balance(c) == 4                               # charged 1, released 3
    assert billing.settle(res) == 1                      # late settle is a no-op
    assert balance(c) == 4
    billing.heartbeat()
    assert_ledger_consistent(database_url)


# -- checkout & webhooks -----------------------------------------------------------------------
def test_checkout_uses_server_side_pack_and_customer(stripe_sessions):
    c = new_customer()
    co = checkout(c, "big")
    assert co["credits"] == 100 and co["checkout_url"].startswith("https://checkout.stripe.com/")
    call = stripe_sessions[-1]
    assert call["line_items"] == [{"price": "price_test_big", "quantity": 1}]
    assert call["client_reference_id"] == c and call["metadata"]["purchase_id"] == co["purchase_id"]
    assert call["api_key"] == "sk_test_dummy" and call["mode"] == "payment"
    assert client.post("/v1/account/credits/checkout", headers=site(c),
                       json={"pack_id": "nope"}).json()["error"] == "unknown_pack"
    r = client.post("/v1/account/credits/checkout", headers=site(c),
                    json={"pack_id": "big", "credits": 999999, "price_id": "price_cheap"})
    assert r.status_code == 422                          # client cannot choose price or amount
    assert balance(c) == 0                               # creating a session adds nothing


def test_successful_payment_restores_access(stripe_sessions):
    c = new_customer()
    k = new_key(c)["secret"]
    assert execute(k, fuzz(1)).status_code == 402
    buy(c)
    r = execute(k, fuzz(1))
    assert r.status_code == 200 and r.json()["results"][0]["status"] == "ok"
    assert balance(c) == 4


def test_duplicate_and_concurrent_webhooks_credit_once(stripe_sessions, database_url):
    c = new_customer()
    co = checkout(c)
    sid = co["checkout_session_id"]
    same = session_event("checkout.session.completed", sid, event_id="evt_dup_" + secrets.token_hex(6))
    assert deliver(same).json()["result"] == "credited"
    assert deliver(same).json()["result"] == "duplicate_event"          # Stripe redelivery
    other = session_event("checkout.session.async_payment_succeeded", sid)
    assert deliver(other).json()["result"] == "already_credited"        # different event, same purchase
    assert balance(c) == 5

    co2 = checkout(c)                                    # now 10 concurrent deliveries of one purchase
    payloads = [session_event("checkout.session.completed", co2["checkout_session_id"],
                              event_id="evt_race_" + co2["purchase_id"])] * 5 + \
               [session_event("checkout.session.completed", co2["checkout_session_id"]) for _ in range(5)]
    barrier = threading.Barrier(len(payloads))

    def send(p):
        barrier.wait()
        return deliver(p).json()["result"]

    with ThreadPoolExecutor(len(payloads)) as pool:
        results = list(pool.map(send, payloads))
    assert results.count("credited") == 1, results
    assert balance(c) == 10
    with psycopg.connect(database_url) as conn:
        n = conn.execute("SELECT count(*) FROM credit_ledger WHERE kind = 'purchase' AND customer_id = %s",
                         (c,)).fetchone()[0]
    assert n == 2
    assert_ledger_consistent(database_url)


def test_cancelled_or_unpaid_sessions_add_zero(stripe_sessions):
    c = new_customer()
    s1, s2, s3 = (checkout(c)["checkout_session_id"] for _ in range(3))
    assert deliver(session_event("checkout.session.completed", s1, payment_status="unpaid")).json()["result"] == "not_paid"
    assert deliver(session_event("checkout.session.expired", s2, payment_status="unpaid")).json()["result"] == "no_credit_expired"
    assert deliver(session_event("checkout.session.async_payment_failed", s3, payment_status="unpaid")).json()["result"] == "no_credit_failed"
    assert deliver(session_event("checkout.session.completed", s3,
                                 payment_status="no_payment_required")).json()["result"] == "not_paid"
    assert balance(c) == 0
    statuses = {p["status"] for p in client.get("/v1/account/credits", headers=site(c)).json()["recent_purchases"]}
    assert statuses == {"pending", "expired", "failed"}
    # s1 was unpaid at completion (async method); it credits once the payment succeeds.
    assert deliver(session_event("checkout.session.async_payment_succeeded", s1)).json()["result"] == "credited"
    assert balance(c) == 5


def test_webhook_signature_is_verified_against_raw_body(stripe_sessions):
    c = new_customer()
    sid = checkout(c)["checkout_session_id"]
    payload = session_event("checkout.session.completed", sid)
    assert deliver(payload, sign(payload, secret="whsec_wrong")).status_code == 400
    tampered = payload.replace(b'"amount_total": 500', b'"amount_total": 1')
    assert deliver(tampered, sign(payload)).status_code == 400        # body changed after signing
    assert deliver(payload, sign(payload, t=int(time.time()) - 3600)).status_code == 400  # stale timestamp
    assert client.post("/v1/stripe/webhook", content=payload).status_code == 400          # no signature
    assert balance(c) == 0


def test_webhooks_for_unknown_sessions_or_wrong_mode_add_nothing(stripe_sessions):
    c = new_customer()
    sid = checkout(c)["checkout_session_id"]
    assert deliver(session_event("checkout.session.completed", "cs_test_not_ours")).json()["result"] == "unknown_session"
    assert deliver(session_event("checkout.session.completed", sid, livemode=True)).json()["result"] == "ignored_mode_mismatch"
    forged = session_event("checkout.session.completed", "cs_test_other",
                           metadata={"purchase_id": str(uuid.uuid4())}, customer=c)
    assert deliver(forged).json()["result"] == "unknown_session"
    assert balance(c) == 0


# -- modes ---------------------------------------------------------------------------------------
def test_billing_off_charges_nothing_and_disables_checkout(monkeypatch, stripe_sessions):
    monkeypatch.setenv("BILLING_MODE", "off")
    c = new_customer()
    k = new_key(c)["secret"]
    assert execute(k, fuzz(2)).status_code == 200 and balance(c) == 0
    r = client.post("/v1/account/credits/checkout", headers=site(c), json={"pack_id": "starter"})
    assert r.status_code == 503 and r.json()["error"] == "billing_disabled"
    payload = session_event("checkout.session.completed", "cs_test_x")
    assert deliver(payload).status_code == 503 and stripe_sessions == []


def test_live_mode_refuses_test_keys(monkeypatch, stripe_sessions):
    monkeypatch.setenv("BILLING_MODE", "live")
    r = client.post("/v1/account/credits/checkout", headers=site(new_customer()), json={"pack_id": "starter"})
    assert r.status_code == 503 and r.json()["error"] == "billing_misconfigured" and stripe_sessions == []
    assert "sk_test_dummy" not in r.text


def test_credits_endpoint_requires_website_auth():
    assert client.get("/v1/account/credits", headers={"X-Customer-Id": "x"}).status_code == 401
    assert client.post("/v1/account/credits/checkout", headers={"X-Customer-Id": "x"},
                       json={"pack_id": "starter"}).status_code == 401


def test_admin_adjust_is_operator_only():
    c = new_customer()
    body = {"customer_id": c, "delta": 7, "note": "support grant"}
    assert client.post("/v1/admin/credits/adjust", json=body).status_code == 401
    assert client.post("/v1/admin/credits/adjust", json=body, headers={"X-API-Key": "nope"}).status_code == 401
    r = client.post("/v1/admin/credits/adjust", json=body, headers={"X-API-Key": OPERATOR_KEY})
    assert r.status_code == 200 and r.json()["balance"] == 7
    r = client.post("/v1/admin/credits/adjust", json={**body, "delta": -8}, headers={"X-API-Key": OPERATOR_KEY})
    assert r.status_code == 409 and balance(c) == 7


def test_migrating_a_pre_migration_database_keeps_data(database_url):
    """Railway's database was created before migrations were tracked; upgrading must keep its data."""
    from app.migrations import V1_ACCOUNTS_KEYS_USAGE, migrate
    base = database_url.rsplit("/", 1)[0]
    name = "legacy_" + uuid.uuid4().hex[:8]
    with psycopg.connect(base + "/postgres", autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name}")
    try:
        with psycopg.connect(f"{base}/{name}") as conn:  # the old app's schema and some data
            conn.execute(V1_ACCOUNTS_KEYS_USAGE)
            conn.execute("INSERT INTO customers (id) VALUES ('legacy-cust')")
            kid = uuid.uuid4()
            conn.execute("INSERT INTO api_keys (id, customer_id, name, prefix, key_hash) "
                         "VALUES (%s, 'legacy-cust', 'old', 'uak_old', %s)", (kid, b"h" * 32))
            conn.execute("INSERT INTO usage_requests (id, customer_id, key_id, task_count, ok_count, error_count, "
                         "duration_ms) VALUES (%s, 'legacy-cust', %s, 1, 1, 0, 1.0)", (uuid.uuid4(), kid))
        with psycopg.connect(f"{base}/{name}") as conn:
            assert migrate(conn) == [1, 2]
        with psycopg.connect(f"{base}/{name}") as conn:
            assert conn.execute("SELECT balance FROM credit_accounts WHERE customer_id = 'legacy-cust'").fetchone() == (0,)
            assert conn.execute("SELECT count(*), sum(credits_charged) FROM usage_requests").fetchone() == (1, 0)
            assert conn.execute("SELECT count(*) FROM api_keys").fetchone() == (1,)
            assert migrate(conn) == []
    finally:
        with psycopg.connect(base + "/postgres", autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def test_migrations_are_recorded_and_idempotent(database_url):
    from app.migrations import MIGRATIONS, migrate
    with psycopg.connect(database_url) as conn:
        assert migrate(conn) == []
        versions = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
    assert versions == [v for v, _, _ in MIGRATIONS]
