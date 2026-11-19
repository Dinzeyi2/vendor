"""Prepaid credits against a real local Postgres (pgserver).

Payments happen on the website; its server reports each confirmed payment to
POST /v1/credits/grant, which must credit every payment_id exactly once.
"""
import secrets
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import billing, db
from app.accounts import KeyOwner
from app.main import app
from app.registry import OPERATIONS, WORKERS, OpSpec
from app.workers import WorkerFailure, WorkerProcess

pgserver = pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

SERVER_SECRET = "test-website-backend-secret-" + "y" * 16
GRANT_SECRET = "test-credits-grant-secret-" + "g" * 16
OPERATOR_KEY = "operator-test-key"
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
                 "CREDITS_GRANT_SECRET": GRANT_SECRET, "BILLING_MODE": "on"}.items():
        monkeypatch.setenv(k, v)
    yield
    db.close()


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


def grant_call(customer, credits, payment_id, secret=GRANT_SECRET, **extra):
    return client.post("/v1/credits/grant", headers={"Authorization": f"Bearer {secret}"},
                       json={"customer_id": customer, "credits": credits, "payment_id": payment_id, **extra})


def pay(customer, credits=5):
    """What the website's server does after Stripe confirms a payment."""
    pid = "cs_test_" + secrets.token_hex(12)
    r = grant_call(customer, credits, pid)
    assert r.status_code == 201 and r.json()["status"] == "credited", r.text
    return pid


def assert_ledger_consistent(database_url):
    with psycopg.connect(database_url) as conn:
        bad = conn.execute(
            "SELECT a.customer_id, a.balance, COALESCE(sum(l.delta), 0) FROM credit_accounts a "
            "LEFT JOIN credit_ledger l ON l.customer_id = a.customer_id "
            "GROUP BY a.customer_id, a.balance HAVING a.balance <> COALESCE(sum(l.delta), 0)").fetchall()
        held = conn.execute("SELECT count(*) FROM credit_reservations WHERE status = 'held'").fetchone()[0]
    assert bad == [], f"balance != sum(ledger) for {bad}"
    assert held == 0, "reservations left held"


# -- grants: the website -> Railway channel ------------------------------------------------------
def test_grant_requires_its_own_secret():
    c = new_customer()
    k = new_key(c)["secret"]
    for secret in ("wrong-" + GRANT_SECRET, SERVER_SECRET, OPERATOR_KEY, k):
        assert grant_call(c, 5, "pay_1", secret=secret).status_code == 401
    assert client.post("/v1/credits/grant", json={"customer_id": c, "credits": 5, "payment_id": "p"}).status_code == 401
    assert balance(c) == 0


@pytest.mark.parametrize("value", ["", "short", SERVER_SECRET, OPERATOR_KEY + "x" * 40])
def test_grant_disabled_unless_secret_is_strong_and_distinct(monkeypatch, value):
    monkeypatch.setenv("CREDITS_GRANT_SECRET", value)
    if value == OPERATOR_KEY + "x" * 40:          # a distinct, strong secret is accepted
        monkeypatch.setenv("API_KEY", value)       # ...unless it equals the operator key
    r = grant_call(new_customer(), 5, "p_" + secrets.token_hex(4), secret=value or "x")
    assert r.status_code == 503


def test_successful_payment_restores_access():
    c = new_customer()
    k = new_key(c)["secret"]
    r = execute(k, fuzz(1))
    assert r.status_code == 402 and r.json()["error"] == "insufficient_credits"
    pay(c, 5)
    r = execute(k, fuzz(1))
    assert r.status_code == 200 and r.json()["results"][0]["status"] == "ok"
    assert balance(c) == 4


def test_duplicate_grants_never_add_extra_credits(database_url):
    c = new_customer()
    pid = pay(c, 5)
    r = grant_call(c, 5, pid)                      # website retries the same payment
    assert r.status_code == 200 and r.json()["status"] == "already_credited" and r.json()["balance"] == 5
    assert balance(c) == 5
    # Same payment_id with a different amount or customer is refused and changes nothing.
    assert grant_call(c, 500, pid).json()["error"] == "payment_id_conflict"
    other = new_customer()
    assert grant_call(other, 5, pid).status_code == 409
    assert (balance(c), balance(other)) == (5, 0)
    assert_ledger_consistent(database_url)


def test_concurrent_duplicate_grants_credit_once(database_url):
    c = new_customer()
    pid = "pi_race_" + secrets.token_hex(8)
    barrier = threading.Barrier(25)

    def send(_):
        barrier.wait()
        return grant_call(c, 7, pid).json()["status"]

    with ThreadPoolExecutor(25) as pool:
        results = list(pool.map(send, range(25)))
    assert results.count("credited") == 1 and results.count("already_credited") == 24
    assert balance(c) == 7
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT count(*) FROM credit_ledger WHERE payment_id = %s", (pid,)).fetchone()[0] == 1
    assert_ledger_consistent(database_url)


def test_grant_validation():
    c = new_customer()
    bad = [{"credits": 0}, {"credits": -5}, {"credits": 10**12}, {"customer_id": "bad id/.."},
           {"payment_id": ""}, {"payment_id": "has space"}, {"payment_id": "x" * 256}, {"extra": 1}]
    for override in bad:
        body = {"customer_id": c, "credits": 5, "payment_id": "p_" + secrets.token_hex(4), **override}
        r = client.post("/v1/credits/grant", headers={"Authorization": f"Bearer {GRANT_SECRET}"}, json=body)
        assert r.status_code == 422, override
    assert balance(c) == 0


def test_grant_before_customer_has_keys():
    c = new_customer()
    pay(c, 3)                                       # paid on the website before creating a key
    k = new_key(c)["secret"]
    assert balance(c) == 3 and execute(k, fuzz(3)).status_code == 200 and balance(c) == 0


def test_credits_endpoint_lists_grants():
    c = new_customer()
    pid = pay(c, 9)
    body = client.get("/v1/account/credits", headers=site(c)).json()
    assert body["balance"] == 9 and body["billing_mode"] == "on"
    assert body["recent_grants"][0]["payment_id"] == pid and body["recent_ledger"][0]["kind"] == "grant"


# -- balances & keys ---------------------------------------------------------------------------
def test_new_account_starts_at_zero_and_key_changes_never_touch_balance(database_url):
    c = new_customer()
    assert balance(c) == 0
    k1 = new_key(c)
    assert balance(c) == 0
    pay(c, 5)
    k2 = new_key(c)
    client.post(f"/v1/account/keys/{k1['id']}/revoke", headers=site(c))
    client.post(f"/v1/account/keys/{k2['id']}/revoke", headers=site(c))
    new_key(c)
    assert balance(c) == 5
    assert_ledger_consistent(database_url)


def test_multiple_keys_share_one_balance(database_url):
    c = new_customer()
    k1, k2 = new_key(c)["secret"], new_key(c)["secret"]
    pay(c, 5)
    assert execute(k1, fuzz(2)).status_code == 200      # 3 left
    assert execute(k2, fuzz(3)).status_code == 200      # 0 left
    assert balance(c) == 0
    for k in (k1, k2):
        r = execute(k, fuzz(1))
        assert r.status_code == 402 and r.json()["error"] == "insufficient_credits"
    assert_ledger_consistent(database_url)


def test_two_customers_stay_isolated(database_url):
    a, b = new_customer(), new_customer()
    ka, kb = new_key(a)["secret"], new_key(b)["secret"]
    pay(a, 5)
    assert (balance(a), balance(b)) == (5, 0)
    assert execute(kb, fuzz(1)).status_code == 402      # A's credits never pay for B
    assert execute(ka, fuzz(1)).status_code == 200
    assert (balance(a), balance(b)) == (4, 0)
    credits_b = client.get("/v1/account/credits", headers=site(b)).json()
    assert credits_b["recent_grants"] == [] and credits_b["recent_ledger"] == []
    assert_ledger_consistent(database_url)


# -- charging rules ----------------------------------------------------------------------------
def test_insufficient_credits_blocks_and_runs_nothing(monkeypatch, database_url):
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
    pay(c, 5)
    r = execute(k, fuzz(6))                              # batch needs 6
    assert r.status_code == 402 and r.json()["required"] == 6 and r.json()["balance"] == 5
    assert ran == [] and balance(c) == 5                 # nothing ran, nothing charged
    assert client.get("/v1/account/usage", headers=site(c)).json()["totals"]["requests"] == 0
    assert execute(k, fuzz(5)).status_code == 200 and len(ran) == 5 and balance(c) == 0
    assert_ledger_consistent(database_url)


def test_unknown_operations_are_rejected_free(database_url):
    c = new_customer()
    k = new_key(c)["secret"]
    r = execute(k, [{"id": "x", "operation": "nope.op", "inputs": {}}])
    assert r.status_code == 200 and r.json()["results"][0]["error_type"] == "unknown_operation"
    pay(c, 5)
    r = execute(k, fuzz(1) + [{"id": "x", "operation": "nope.op", "inputs": {}}])
    assert r.status_code == 200 and balance(c) == 4      # only the known operation was charged
    assert client.get("/v1/account/usage", headers=site(c)).json()["totals"]["credits_charged"] == 1
    assert_ledger_consistent(database_url)


def test_started_failures_are_charged_and_unstarted_work_is_refunded(monkeypatch, database_url):
    def fail_after_start(op, inputs, timeout, on_start=None):
        on_start()
        raise WorkerFailure("worker_crashed", "worker 'scs' exited (code 1) while running scs.solve", started=True)

    def fail_before_start(op, inputs, timeout, on_start=None):
        raise WorkerFailure("timeout", "worker 'xgboost' stayed busy for 1s", started=False)

    monkeypatch.setattr(WORKERS["scs"], "call", fail_after_start)
    monkeypatch.setattr(WORKERS["xgboost"], "call", fail_before_start)
    c = new_customer()
    k = new_key(c)["secret"]
    pay(c, 5)
    r = execute(k, [{"id": "a", "operation": "scs.solve", "inputs": {}},
                    {"id": "b", "operation": "xgboost.train_predict", "inputs": {}},
                    *fuzz(1)])
    assert r.status_code == 200
    assert [t["status"] for t in r.json()["results"]] == ["error", "error", "ok"]
    assert balance(c) == 3                               # scs (started, failed) + fuzz charged; xgboost refunded
    assert_ledger_consistent(database_url)


def test_real_worker_crash_and_restart(monkeypatch, database_url):
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
        pay(c, 5)
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


def test_operator_key_is_never_charged():
    r = execute(OPERATOR_KEY, fuzz(3))
    assert r.status_code == 200 and all(t["status"] == "ok" for t in r.json()["results"])


# -- concurrency -------------------------------------------------------------------------------
def test_concurrent_reservations_cannot_overspend(database_url):
    c = new_customer()
    key = new_key(c)
    pay(c, 10)
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


def test_concurrent_http_requests_cannot_overspend(database_url):
    c = new_customer()
    k1, k2 = new_key(c)["secret"], new_key(c)["secret"]
    pay(c, 5)
    with TestClient(app) as tc, ThreadPoolExecutor(20) as pool:
        codes = list(pool.map(lambda i: tc.post("/v1/execute", headers={"X-API-Key": (k1, k2)[i % 2]},
                                                json={"tasks": fuzz(2)}).status_code, range(20)))
    assert codes.count(200) == 2 and codes.count(402) == 18   # 2 x 2 credits fit in 5
    assert balance(c) == 1
    assert_ledger_consistent(database_url)


def test_spending_and_granting_at_the_same_time(database_url):
    """Grants arriving while keys are spending: every credit is accounted for."""
    c = new_customer()
    k = new_key(c)["secret"]
    pay(c, 10)
    with TestClient(app) as tc, ThreadPoolExecutor(30) as pool:
        def spend(_):
            return tc.post("/v1/execute", headers={"X-API-Key": k}, json={"tasks": fuzz(1)}).status_code

        def topup(i):
            return grant_call(c, 1, f"topup-{c}-{i}").status_code

        spends = [pool.submit(spend, i) for i in range(30)]
        topups = [pool.submit(topup, i) for i in range(10)]
        ok = sum(f.result() == 200 for f in spends)
        assert all(f.result() == 201 for f in topups)
    assert balance(c) == 10 + 10 - ok and ok <= 20
    assert_ledger_consistent(database_url)


# -- crash recovery ----------------------------------------------------------------------------
def test_sweeper_releases_reservations_of_dead_process(database_url):
    c = new_customer()
    key = new_key(c)
    pay(c, 5)
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


# -- modes & operator ------------------------------------------------------------------------------
def test_billing_off_charges_nothing_but_grants_still_work(monkeypatch):
    monkeypatch.setenv("BILLING_MODE", "off")
    c = new_customer()
    k = new_key(c)["secret"]
    assert execute(k, fuzz(2)).status_code == 200 and balance(c) == 0
    pay(c, 4)                                            # balances can be loaded before enforcement
    assert execute(k, fuzz(2)).status_code == 200 and balance(c) == 4
    assert client.get("/v1/account/credits", headers=site(c)).json()["billing_mode"] == "off"


def test_credits_endpoint_requires_website_auth():
    assert client.get("/v1/account/credits", headers={"X-Customer-Id": "x"}).status_code == 401
    r = client.get("/v1/account/credits", headers={"Authorization": f"Bearer {GRANT_SECRET}", "X-Customer-Id": "x"})
    assert r.status_code == 401                          # the grant secret cannot read balances


def test_stripe_endpoints_are_gone():
    assert client.post("/v1/account/credits/checkout", headers=site("x"), json={"pack_id": "a"}).status_code in (404, 405)
    assert client.post("/v1/stripe/webhook", content=b"{}").status_code == 404


def test_admin_adjust_is_operator_only():
    c = new_customer()
    body = {"customer_id": c, "delta": 7, "note": "support grant"}
    assert client.post("/v1/admin/credits/adjust", json=body).status_code == 401
    assert client.post("/v1/admin/credits/adjust", json=body, headers={"X-API-Key": "nope"}).status_code == 401
    r = client.post("/v1/admin/credits/adjust", json=body, headers={"X-API-Key": OPERATOR_KEY})
    assert r.status_code == 200 and r.json()["balance"] == 7
    r = client.post("/v1/admin/credits/adjust", json={**body, "delta": -8}, headers={"X-API-Key": OPERATOR_KEY})
    assert r.status_code == 409 and balance(c) == 7


# -- migrations ----------------------------------------------------------------------------------
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
            assert migrate(conn) == [1, 2, 3]
        with psycopg.connect(f"{base}/{name}") as conn:
            assert conn.execute("SELECT balance FROM credit_accounts WHERE customer_id = 'legacy-cust'").fetchone() == (0,)
            assert conn.execute("SELECT count(*), sum(credits_charged) FROM usage_requests").fetchone() == (1, 0)
            assert conn.execute("SELECT count(*) FROM api_keys").fetchone() == (1,)
            tables = {r[0] for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
            assert "credit_grants" in tables and not tables & {"credit_purchases", "stripe_events"}
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
