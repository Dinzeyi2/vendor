"""Customer keys and usage against a real local Postgres (pgserver). Skipped if unavailable."""
import secrets
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.registry import WORKERS
from app.workers import WorkerFailure

pgserver = pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

SERVER_SECRET = "test-website-backend-secret-" + "x" * 16
OPERATOR_KEY = "operator-test-key"
client = TestClient(app)


@pytest.fixture(scope="module")
def database_url():
    data = Path(__file__).resolve().parent.parent / ".runtimes" / "pgdata"
    srv = pgserver.get_server(str(data), cleanup_mode=None)
    name = "t_" + uuid.uuid4().hex[:12]
    with psycopg.connect(srv.get_uri(), autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name}")
    base = srv.get_uri().rsplit("/", 1)[0]
    yield f"{base}/{name}"
    db.close()
    with psycopg.connect(srv.get_uri(), autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


@pytest.fixture(autouse=True)
def env(monkeypatch, database_url):
    monkeypatch.setenv("DATABASE_URL", database_url)
    monkeypatch.setenv("WEBSITE_BACKEND_SECRET", SERVER_SECRET)
    monkeypatch.setenv("API_KEY", OPERATOR_KEY)


def site(customer, secret=SERVER_SECRET):
    return {"Authorization": f"Bearer {secret}", "X-Customer-Id": customer}


def new_customer():
    return "cust_" + secrets.token_hex(6)


def create(customer, name="k"):
    r = client.post("/v1/account/keys", headers=site(customer), json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def execute(key, tasks):
    return client.post("/v1/execute", headers={"X-API-Key": key}, json={"tasks": tasks})


FUZZ = {"id": "t-Drift", "operation": "rapidfuzz.compare", "inputs": {"a": "Drift Protein", "b": "Drift Proteins"}}


def test_create_returns_secret_once_and_list_shows_metadata_only():
    c = new_customer()
    created = create(c, "Production")
    assert created["secret"].startswith("uak_") and created["prefix"] == created["secret"][:12]
    assert created["status"] == "active" and created["last_used_at"] is None
    listed = client.get("/v1/account/keys", headers=site(c)).json()["keys"]
    assert [k["id"] for k in listed] == [created["id"]]
    assert set(listed[0]) == {"id", "name", "prefix", "status", "created_at", "last_used_at", "revoked_at"}
    assert created["secret"] not in str(listed)


def test_server_credential_is_required_and_separate():
    c = new_customer()
    key = create(c)["secret"]
    for headers in ({"X-Customer-Id": c},                                     # no credential
                    site(c, "wrong-" + SERVER_SECRET),                        # wrong credential
                    site(c, key),                                             # a customer key
                    site(c, OPERATOR_KEY)):                                   # the operator key
        assert client.get("/v1/account/keys", headers=headers).status_code == 401
    assert client.get("/v1/account/keys", headers={"Authorization": f"Bearer {SERVER_SECRET}"}).status_code == 400
    assert client.get("/v1/account/keys", headers=site("bad id/../x")).status_code == 400
    # The server credential is not an algorithm key either.
    assert execute(SERVER_SECRET, [FUZZ]).status_code == 401


def test_customer_id_in_body_is_rejected():
    a, b = new_customer(), new_customer()
    r = client.post("/v1/account/keys", headers=site(a), json={"name": "x", "customer_id": b})
    assert r.status_code == 422
    assert client.get("/v1/account/keys", headers=site(b)).json()["keys"] == []


def test_customers_are_isolated():
    a, b = new_customer(), new_customer()
    ka, kb = create(a, "A key"), create(b, "B key")
    assert [k["id"] for k in client.get("/v1/account/keys", headers=site(b)).json()["keys"]] == [kb["id"]]
    # B cannot revoke or read usage for A's key, and gets the same 404 as for a missing key.
    assert client.post(f"/v1/account/keys/{ka['id']}/revoke", headers=site(b)).status_code == 404
    assert client.post(f"/v1/account/keys/{uuid.uuid4()}/revoke", headers=site(b)).status_code == 404
    assert client.get("/v1/account/usage", headers=site(b), params={"key_id": ka["id"]}).status_code == 404
    assert execute(ka["secret"], [FUZZ]).status_code == 200  # still active
    assert client.get("/v1/account/usage", headers=site(b)).json()["totals"]["requests"] == 0
    assert client.get("/v1/account/usage", headers=site(a)).json()["totals"]["requests"] == 1


def test_usage_recorded_per_request_and_operation(monkeypatch):
    def crash(*_a, **_k):
        raise WorkerFailure("worker_crashed", "worker 'scs' exited (code 1); last stderr: OTHER-CUSTOMER-DATA")
    monkeypatch.setattr(WORKERS["scs"], "call", crash)
    c = new_customer()
    k = create(c)
    r = execute(k["secret"], [FUZZ, {"id": "t2", "operation": "not.real", "inputs": {"secret": "zzz-input"}},
                              {"id": "t3", "operation": "scs.solve", "inputs": {}}])
    assert r.status_code == 200
    by_id = {t["id"]: t for t in r.json()["results"]}
    assert by_id["t-Drift"]["status"] == "ok"
    assert "OTHER-CUSTOMER-DATA" not in by_id["t3"]["error"]  # worker stderr hidden from customers

    u = client.get("/v1/account/usage", headers=site(c)).json()
    assert u["totals"] == {"requests": 1, "operations": 3, "operations_ok": 1, "operations_error": 2,
                           "credits_charged": 0}  # BILLING_MODE unset: nothing is charged
    assert {o["operation"] for o in u["by_operation"]} == {"rapidfuzz.compare", "unknown", "scs.solve"}
    req = u["recent_requests"][0]
    assert req["key_id"] == k["id"] and req["task_count"] == 3 and len(req["operations"]) == 3
    assert {o["error_type"] for o in req["operations"]} == {None, "unknown_operation", "worker_crashed"}
    assert client.get("/v1/account/keys", headers=site(c)).json()["keys"][0]["last_used_at"] is not None


def test_revoked_key_gets_401_and_history_is_kept():
    c = new_customer()
    keep, gone = create(c, "keep"), create(c, "gone")
    assert execute(gone["secret"], [FUZZ]).status_code == 200
    r = client.post(f"/v1/account/keys/{gone['id']}/revoke", headers=site(c))
    assert r.status_code == 200 and r.json()["status"] == "revoked" and r.json()["revoked_at"]
    assert execute(gone["secret"], [FUZZ]).status_code == 401
    assert client.post(f"/v1/account/keys/{gone['id']}/revoke", headers=site(c)).status_code == 200  # idempotent
    assert execute(keep["secret"], [FUZZ]).status_code == 200
    u = client.get("/v1/account/usage", headers=site(c)).json()
    by_key = {k["key_id"]: k for k in u["by_key"]}
    assert by_key[gone["id"]] == {**by_key[gone["id"]], "status": "revoked", "requests": 1, "operations": 1}
    assert by_key[keep["id"]]["requests"] == 1
    assert u["totals"]["requests"] == 2


def test_operator_key_still_works_and_is_not_customer_usage():
    assert execute(OPERATOR_KEY, [FUZZ]).status_code == 200
    ops = client.get("/v1/operations", headers={"X-API-Key": OPERATOR_KEY}).json()["operations"]
    assert "worker_notes" in ops[0]


def test_customers_do_not_see_worker_internals():
    k = create(new_customer())["secret"]
    ops = client.get("/v1/operations", headers={"X-API-Key": k}).json()["operations"]
    assert len(ops) == 10 and "worker_notes" not in ops[0] and "runs_in" not in ops[0]


def test_storage_holds_no_secrets_or_task_data(database_url):
    c = new_customer()
    k = create(c)
    marker = "INPUT-MARKER-" + secrets.token_hex(4)
    execute(k["secret"], [{"id": "task-" + marker, "operation": "rapidfuzz.compare",
                           "inputs": {"a": marker, "b": marker + "x"}}])
    with psycopg.connect(database_url) as conn:
        dump = " ".join(str(row) for table in ("customers", "api_keys", "usage_requests", "usage_operations")
                        for row in conn.execute(f"SELECT * FROM {table}").fetchall())
    assert k["secret"] not in dump
    assert k["secret"][12:] not in dump      # no part of the secret beyond the display prefix
    assert marker not in dump                # no inputs, outputs or task ids


def test_unknown_or_malformed_customer_keys_are_401():
    assert execute("uak_" + secrets.token_urlsafe(32), [FUZZ]).status_code == 401
    assert execute("nope", [FUZZ]).status_code == 401
    assert client.post("/v1/execute", json={"tasks": [FUZZ]}).status_code == 401
