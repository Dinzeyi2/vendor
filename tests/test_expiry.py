"""Credit expiration: lots, spend order, boundaries, refunds, duplicates, migration report.

Runs against a real local Postgres (pgserver). Time-sensitive tests pin the billing clock with
billing._clock; expiry dates themselves are computed by Postgres.
"""
import secrets
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import billing, db, migrations
from app.accounts import KeyOwner
from app.main import app

pgserver = pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

SERVER_SECRET = "test-website-backend-secret-" + "e" * 16
GRANT_SECRET = "test-credits-grant-secret-" + "f" * 16
OPERATOR_KEY = "operator-test-key-expiry"
client = TestClient(app)
UTC = timezone.utc


@pytest.fixture(scope="module")
def pg():
    data = Path(__file__).resolve().parent.parent / ".runtimes" / "pgdata"
    return pgserver.get_server(str(data), cleanup_mode=None)


@pytest.fixture(scope="module")
def database_url(pg):
    name = "x_" + uuid.uuid4().hex[:12]
    with psycopg.connect(pg.get_uri(), autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name}")
    yield pg.get_uri().rsplit("/", 1)[0] + "/" + name
    db.close()
    with psycopg.connect(pg.get_uri(), autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture(autouse=True)
def env(monkeypatch, database_url):
    db.close()
    for k, v in {"DATABASE_URL": database_url, "WEBSITE_BACKEND_SECRET": SERVER_SECRET, "API_KEY": OPERATOR_KEY,
                 "CREDITS_GRANT_SECRET": GRANT_SECRET, "BILLING_MODE": "on"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(billing, "_clock", None)
    yield
    db.close()


def at(when):
    """Pin the billing clock."""
    billing._clock = (lambda: when)


def utc(*args):
    return datetime(*args, tzinfo=UTC)


def iso(dt):
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def site(c):
    return {"Authorization": f"Bearer {SERVER_SECRET}", "X-Customer-Id": c}


def customer():
    return "exp_" + secrets.token_hex(6)


def key(c):
    r = client.post("/v1/account/keys", headers=site(c), json={"name": "k"})
    assert r.status_code == 201
    return r.json()


def grant(c, credits, purchased_at=None, type_="paid", payment_id=None):
    body = {"customer_id": c, "credits": credits, "payment_id": payment_id or "pay_" + secrets.token_hex(8),
            "type": type_}
    if purchased_at is not None:
        body["purchased_at"] = iso(purchased_at)
    return client.post("/v1/credits/grant", headers={"Authorization": f"Bearer {GRANT_SECRET}"}, json=body)


def credits(c):
    r = client.get("/v1/account/credits", headers=site(c))
    assert r.status_code == 200
    return r.json()


def fuzz(n):
    return [{"id": f"t{i}", "operation": "rapidfuzz.compare", "inputs": {"a": "x", "b": "y"}} for i in range(n)]


def execute(k, n):
    return client.post("/v1/execute", headers={"X-API-Key": k}, json={"tasks": fuzz(n)})


def lots(database_url, c):
    with psycopg.connect(database_url) as conn:
        return conn.execute("SELECT source, expires_at, original, remaining FROM credit_lots WHERE customer_id = %s "
                            "ORDER BY id", (c,)).fetchall()


def consistent(database_url):
    with psycopg.connect(database_url) as conn:
        bad = conn.execute(
            "SELECT a.customer_id FROM credit_accounts a "
            "LEFT JOIN (SELECT customer_id, sum(delta) d FROM credit_ledger GROUP BY customer_id) l USING (customer_id) "
            "LEFT JOIN (SELECT customer_id, sum(remaining) r FROM credit_lots GROUP BY customer_id) t USING (customer_id) "
            "WHERE a.balance <> COALESCE(l.d, 0) OR a.balance <> COALESCE(t.r, 0)").fetchall()
        held = conn.execute("SELECT count(*) FROM credit_reservations WHERE status = 'held'").fetchone()[0]
    assert bad == [] and held == 0, (bad, held)


# -- expiry dates -----------------------------------------------------------------------------
def test_paid_grant_expires_one_calendar_year_after_purchase():
    c = customer()
    r = grant(c, 5, utc(2026, 3, 15, 10, 30, 0))
    assert r.status_code == 201
    assert r.json()["expires_at"].startswith("2027-03-15T10:30:00")
    at(utc(2028, 3, 1))                                    # leap day purchase -> Feb 28 next year
    r = grant(c, 5, utc(2028, 2, 29, 12, 0, 0))
    assert r.json()["expires_at"].startswith("2029-02-28T12:00:00")
    r = grant(c, 5, datetime(2026, 11, 19, 23, 30, tzinfo=timezone(timedelta(hours=-5))))  # offsets honoured
    assert r.json()["expires_at"].startswith("2027-11-20T04:30:00")


def test_purchase_timestamp_rules():
    c = customer()
    r = grant(c, 5, datetime.now(UTC) + timedelta(hours=1))
    assert r.status_code == 422 and r.json()["error"] == "purchased_at_in_future"
    r = grant(c, 5, datetime.now(UTC) - timedelta(days=1))
    assert r.status_code == 201 and r.json()["purchased_at_source"] == "verified"
    assert grant(c, 5, type_="welcome").status_code == 201                 # welcome needs no timestamp
    assert credits(c)["balance"] == 10


def test_backward_compatible_with_the_current_grant_payload(database_url):
    """Lovable's existing webhook sends only customer_id, credits and payment_id."""
    c = customer()
    k = key(c)["secret"]
    pid = "cs_test_legacy_payload_" + secrets.token_hex(6)
    old = {"customer_id": c, "credits": 4, "payment_id": pid}
    r = client.post("/v1/credits/grant", headers={"Authorization": f"Bearer {GRANT_SECRET}"}, json=old)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "credited" and body["type"] == "paid" and body["purchased_at_source"] == "received"
    received = datetime.fromisoformat(body["purchased_at"])
    assert abs((received - datetime.now(UTC)).total_seconds()) < 60        # Railway's receive time
    assert datetime.fromisoformat(body["expires_at"]).year == received.year + 1
    r = client.post("/v1/credits/grant", headers={"Authorization": f"Bearer {GRANT_SECRET}"}, json=old)
    assert r.status_code == 200 and r.json()["status"] == "already_credited"
    # A later, upgraded retry of the same payment (now with purchased_at) is still the same payment.
    r = grant(c, 4, datetime.now(UTC) - timedelta(minutes=1), payment_id=pid)
    assert r.status_code == 200 and r.json()["purchased_at_source"] == "received"
    assert credits(c)["balance"] == 4 and execute(k, 4).status_code == 200
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT purchased_at_source FROM credit_grants WHERE payment_id = %s",
                            (pid,)).fetchone() == ("received",)
    consistent(database_url)


# -- spend order ------------------------------------------------------------------------------
def test_welcome_credits_are_separate_and_spent_first(database_url):
    c = customer()
    k = key(c)["secret"]
    assert grant(c, 3, type_="welcome").status_code == 201
    assert grant(c, 5, datetime.now(UTC)).status_code == 201
    body = credits(c)
    assert (body["balance"], body["free_balance"], body["paid_balance"]) == (8, 3, 5)
    assert execute(k, 4).status_code == 200
    body = credits(c)
    assert (body["balance"], body["free_balance"], body["paid_balance"]) == (4, 0, 4)
    assert {g["type"] for g in body["recent_grants"]} == {"welcome", "paid"}
    consistent(database_url)


def test_paid_credits_spent_in_earliest_expiration_order(database_url):
    c = customer()
    k = key(c)["secret"]
    now = datetime.now(UTC)
    later = grant(c, 5, now - timedelta(days=1)).json()["expires_at"]      # granted first, expires later
    sooner = grant(c, 5, now - timedelta(days=100)).json()["expires_at"]   # granted second, expires sooner
    assert execute(k, 6).status_code == 200
    rows = {str(e)[:10]: rem for _, e, _, rem in lots(database_url, c)}
    assert rows[sooner[:10]] == 0 and rows[later[:10]] == 4
    nxt = credits(c)["next_expiration"]
    assert nxt["amount"] == 4 and nxt["expires_at"][:10] == later[:10]
    consistent(database_url)


def test_partial_spending_refunds_return_to_the_same_lots(database_url):
    c = customer()
    owner = KeyOwner(c, uuid.UUID(key(c)["id"]))
    now = datetime.now(UTC)
    grant(c, 2, type_="welcome")
    grant(c, 3, now - timedelta(days=200))     # A: expires sooner
    grant(c, 3, now - timedelta(days=10))      # B
    res = billing.reserve(owner, 6, 60)        # takes welcome 2, A 3, B 1
    assert [r for *_, r in lots(database_url, c)] == [0, 0, 2]
    res.started = 3                            # 3 of 6 operations started
    assert billing.settle(res) == 3            # keep welcome 2 + A 1; return A 2 and B 1
    assert [r for *_, r in lots(database_url, c)] == [0, 2, 3]
    assert credits(c)["balance"] == 5
    consistent(database_url)


def test_legacy_credits_are_spent_after_expiring_paid_credits(database_url):
    c = customer()
    k = key(c)["secret"]
    with psycopg.connect(database_url) as conn:  # a pre-expiration balance, as migration 4 creates
        conn.execute("UPDATE credit_accounts SET balance = balance + 4 WHERE customer_id = %s", (c,))
        conn.execute("INSERT INTO credit_lots (customer_id, source, original, remaining) VALUES (%s, 'legacy', 4, 4)", (c,))
        conn.execute("INSERT INTO credit_ledger (customer_id, delta, kind, balance_after) VALUES (%s, 4, 'adjustment', 4)", (c,))
    grant(c, 2, datetime.now(UTC))
    assert execute(k, 3).status_code == 200
    assert [(s, r) for s, _, _, r in lots(database_url, c)] == [("legacy", 3), ("paid", 0)]
    consistent(database_url)


# -- expiration boundary & 402 ------------------------------------------------------------------
def test_expiration_boundary(database_url):
    c = customer()
    k = key(c)["secret"]
    purchased = utc(2025, 12, 1, 9, 0, 0)
    expires = utc(2026, 12, 1, 9, 0, 0)
    at(purchased + timedelta(days=1))
    assert grant(c, 5, purchased).status_code == 201
    at(expires - timedelta(microseconds=1))                # last instant the credits are valid
    body = credits(c)
    assert body["balance"] == 5 and body["next_expiration"]["amount"] == 5
    assert body["next_expiration"]["expires_at"].startswith("2026-12-01T09:00:00")
    assert execute(k, 1).status_code == 200
    at(expires)                                            # expired from expires_at on
    body = credits(c)
    assert body["balance"] == 0 and body["next_expiration"] is None
    r = execute(k, 1)
    assert r.status_code == 402 and r.json()["error"] == "insufficient_credits" and r.json()["balance"] == 0
    assert credits(c)["recent_ledger"][0]["kind"] == "expire"
    consistent(database_url)


def test_402_when_only_expired_credits_remain(database_url):
    c = customer()
    k = key(c)["secret"]
    old = datetime.now(UTC) - timedelta(days=367)
    r = grant(c, 10, old)                                  # reported more than a year after purchase
    assert r.status_code == 201 and r.json()["balance"] == 0 and r.json()["expired_on_arrival"] == 10
    r = execute(k, 1)
    assert r.status_code == 402 and r.json() == {
        "error": "insufficient_credits", "message": r.json()["message"], "required": 1, "balance": 0}
    assert client.get("/v1/account/usage", headers=site(c)).json()["totals"]["requests"] == 0
    assert grant(c, 1, type_="welcome").status_code == 201  # any valid credit restores access
    assert execute(k, 1).status_code == 200
    consistent(database_url)


def test_refunds_cannot_revive_expired_credits(database_url):
    c = customer()
    owner = KeyOwner(c, uuid.UUID(key(c)["id"]))
    purchased, expires = utc(2025, 12, 5), utc(2026, 12, 5)
    at(purchased + timedelta(days=31))
    assert grant(c, 3, purchased).status_code == 201                       # expires first
    assert grant(c, 4, purchased + timedelta(days=30)).status_code == 201  # still valid after the first expires
    at(expires - timedelta(seconds=10))
    res = billing.reserve(owner, 5, 60)                    # takes 3 from the expiring lot, 2 from the other
    at(expires + timedelta(seconds=1))                     # the request runs past the expiry
    res.started = 0                                        # nothing started: everything is "refunded"
    assert billing.settle(res) == 0
    assert credits(c)["balance"] == 4                      # 2 returned to the valid lot; 3 forfeited, not revived
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT forfeited FROM credit_reservations WHERE id = %s", (res.id,)).fetchone() == (3,)
    consistent(database_url)


def test_sweeper_writes_off_expired_lots(database_url):
    c = customer()
    purchased = utc(2025, 12, 10)
    at(purchased)
    grant(c, 6, purchased)
    at(utc(2026, 12, 10, 0, 0, 1))
    assert billing.expire_sweep() >= 6
    with psycopg.connect(database_url) as conn:
        assert conn.execute("SELECT balance FROM credit_accounts WHERE customer_id = %s", (c,)).fetchone() == (0,)
        assert conn.execute("SELECT delta FROM credit_ledger WHERE customer_id = %s AND kind = 'expire'",
                            (c,)).fetchone() == (-6,)
    consistent(database_url)


def test_balance_endpoint_shows_next_expiration():
    c = customer()
    at(utc(2026, 11, 19, 12))
    grant(c, 2, type_="welcome")
    grant(c, 7, utc(2026, 1, 10, 8))
    grant(c, 3, utc(2026, 1, 10, 8))       # same expiry date: amounts are summed
    grant(c, 5, utc(2026, 6, 1, 8))
    body = credits(c)
    assert body["balance"] == 17 and body["free_balance"] == 2 and body["paid_balance"] == 15
    assert body["next_expiration"]["amount"] == 10
    assert body["next_expiration"]["expires_at"].startswith("2027-01-10T08:00:00")
    assert [e["amount"] for e in body["upcoming_expirations"]] == [10, 5]


# -- concurrency & duplicates --------------------------------------------------------------------
def test_concurrent_requests_cannot_spend_expired_or_extra_credits(database_url):
    c = customer()
    owner = KeyOwner(c, uuid.UUID(key(c)["id"]))
    grant(c, 10, datetime.now(UTC) - timedelta(days=400))  # expired
    grant(c, 10, datetime.now(UTC))                        # valid
    barrier = threading.Barrier(40)

    def attempt(_):
        barrier.wait()
        try:
            res = billing.reserve(owner, 3, 60)
        except billing.BillingError:
            return 0
        res.started = 3
        billing.settle(res)
        return 3

    with ThreadPoolExecutor(40) as pool:
        assert sum(pool.map(attempt, range(40))) == 9      # only the 10 valid credits: 3 x 3
    assert credits(c)["balance"] == 1
    consistent(database_url)


def test_duplicate_payment_events_credit_once(database_url):
    c = customer()
    pid = "cs_test_dup_" + secrets.token_hex(6)
    first = datetime.now(UTC) - timedelta(minutes=5)
    r1 = grant(c, 8, first, payment_id=pid)
    assert r1.status_code == 201
    # The same payment arrives again from a later Stripe event with a different timestamp.
    r2 = grant(c, 8, first + timedelta(minutes=3), payment_id=pid)
    assert r2.status_code == 200 and r2.json()["status"] == "already_credited"
    assert r2.json()["expires_at"] == r1.json()["expires_at"]           # the first purchase time stands
    assert grant(c, 8, type_="welcome", payment_id=pid).status_code == 409  # same ID, different type
    barrier = threading.Barrier(20)

    def send(_):
        barrier.wait()
        return grant(c, 8, first, payment_id=pid).json()["status"]

    with ThreadPoolExecutor(20) as pool:
        assert set(pool.map(send, range(20))) == {"already_credited"}
    assert credits(c)["balance"] == 8 and len(lots(database_url, c)) == 1
    consistent(database_url)


# -- migration & report -------------------------------------------------------------------------
def test_migration_preserves_balances_and_report_explains_legacy_grants(pg, monkeypatch):
    base = pg.get_uri().rsplit("/", 1)[0]
    name = "pre4_" + uuid.uuid4().hex[:8]
    with psycopg.connect(base + "/postgres", autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name}")
    url = f"{base}/{name}"
    full = migrations.MIGRATIONS
    try:
        migrations.MIGRATIONS = full[:3]                     # the schema production has now
        with psycopg.connect(url) as conn:
            assert migrations.migrate(conn) == [1, 2, 3]
            conn.execute("INSERT INTO customers (id) VALUES ('old-cust'), ('spent-cust')")
            conn.execute("INSERT INTO credit_accounts (customer_id, balance) VALUES ('old-cust', 120), ('spent-cust', 0)")
            for pid, cust, credits_, days in (("cs_old", "old-cust", 100, 400), ("cs_new", "old-cust", 50, 30),
                                              ("cs_gone", "spent-cust", 20, 10)):
                conn.execute("INSERT INTO credit_grants (payment_id, customer_id, credits, created_at) "
                             "VALUES (%s, %s, %s, now() - make_interval(days => %s))", (pid, cust, credits_, days))
        migrations.MIGRATIONS = full
        with psycopg.connect(url) as conn:
            assert migrations.migrate(conn) == [4, 5]
            assert conn.execute("SELECT source, expires_at, remaining FROM credit_lots").fetchall() == [("legacy", None, 120)]
            assert conn.execute("SELECT grant_type, purchased_at, expires_at FROM credit_grants WHERE payment_id = 'cs_old'"
                                ).fetchone() == ("paid", None, None)                           # terms unchanged
        monkeypatch.setenv("DATABASE_URL", url)
        db.close()
        assert credits("old-cust")["balance"] == 120 and credits("old-cust")["next_expiration"] is None
        assert client.get("/v1/admin/credits/expiry-report").status_code == 401
        report = client.get("/v1/admin/credits/expiry-report", headers={"X-API-Key": OPERATOR_KEY}).json()
        assert report["totals"] == {"legacy_remaining": 120, "would_expire_immediately": 70,
                                    "would_expire_later": 50, "would_never_expire": 0}
        rows = {r["payment_id"]: r for r in report["customers"][0]["inflows"]}
        assert rows["cs_new"]["attributed_remaining"] == 50 and rows["cs_new"]["outcome"] == "would_expire_later"
        assert rows["cs_old"]["attributed_remaining"] == 70 and rows["cs_old"]["outcome"] == "would_expire_immediately"
        assert credits("old-cust")["balance"] == 120                                           # report changed nothing
    finally:
        migrations.MIGRATIONS = full
        db.close()
        with psycopg.connect(base + "/postgres", autocommit=True) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
