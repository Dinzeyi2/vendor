"""Customer GitHub imports end to end, against a real local Postgres.

The repos are this project's own fixture packages (tests/fixtures/import_repos), served in place
of GitHub; they run in the sandbox's insecure-dev mode because the strict lockdown needs Linux +
root (it is exercised on the server by GET /v1/admin/imports/sandbox-selftest).
"""
import hashlib
import json
import shutil
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import accounts, db
from app.imports import github, jobs, private
from app.main import app

pgserver = pytest.importorskip("pgserver")
psycopg = pytest.importorskip("psycopg")

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "import_repos"
SERVER_SECRET = "test-website-backend-secret-" + "y" * 16
GRANT_SECRET = "test-credit-grant-secret-" + "z" * 16
OPERATOR_KEY = "operator-test-key"
client = TestClient(app)


@pytest.fixture(scope="module")
def database_url():
    data = Path(__file__).resolve().parent.parent / ".runtimes" / "pgdata"
    srv = pgserver.get_server(str(data), cleanup_mode=None)
    name = "i_" + uuid.uuid4().hex[:12]
    with psycopg.connect(srv.get_uri(), autocommit=True) as conn:
        conn.execute(f"CREATE DATABASE {name}")
    yield srv.get_uri().rsplit("/", 1)[0] + "/" + name
    db.close()
    with psycopg.connect(srv.get_uri(), autocommit=True) as conn:
        conn.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


def fake_commit(ref):
    return hashlib.sha1(ref.full_name.encode()).hexdigest()


def fake_fetch(ref, commit, dest):
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(FIXTURES / ref.repo, dest)
    return dest


def verify_in_process(customer_id, import_id, operation, inputs):
    """The same check as production (a real POST /v1/execute with a scoped key), via the test client."""
    key = accounts.create_key(customer_id, "Import check (automatic)", kind="import_check", scope=import_id)
    try:
        r = client.post("/v1/execute", headers={"X-API-Key": key["secret"]},
                        json={"tasks": [{"id": "import-check", "operation": operation, "inputs": inputs}]})
        return r.status_code, r.json()
    finally:
        accounts.revoke_key(customer_id, uuid.UUID(key["id"]))


@pytest.fixture(autouse=True)
def env(monkeypatch, database_url, tmp_path):
    db.close()
    for k, v in {"DATABASE_URL": database_url, "WEBSITE_BACKEND_SECRET": SERVER_SECRET, "API_KEY": OPERATOR_KEY,
                 "CREDITS_GRANT_SECRET": GRANT_SECRET, "BILLING_MODE": "on", "COGNEX_SANDBOX": "insecure-dev",
                 "IMPORTS_DIR": str(tmp_path / "imports"), "COGNEX_PUBLIC_URL": "https://cognex.example"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(github, "resolve_commit", fake_commit)
    monkeypatch.setattr(jobs, "FETCH", fake_fetch)
    monkeypatch.setattr(jobs, "VERIFY", verify_in_process)
    private.invalidate()
    yield
    private.invalidate()
    db.close()


def site(customer):
    return {"Authorization": f"Bearer {SERVER_SECRET}", "X-Customer-Id": customer}


def new_customer():
    return "cust-" + uuid.uuid4().hex[:10]


def key_for(customer, credits=10):
    secret = client.post("/v1/account/keys", headers=site(customer), json={"name": "app"}).json()["secret"]
    if credits:
        r = client.post("/v1/credits/grant", headers={"Authorization": f"Bearer {GRANT_SECRET}"},
                        json={"customer_id": customer, "credits": credits, "payment_id": "p-" + uuid.uuid4().hex,
                              "purchased_at": "2026-11-20T00:00:00Z"})
        assert r.status_code == 201, r.text
    return secret


def balance(customer):
    return client.get("/v1/account/credits", headers=site(customer)).json()["balance"]


def submit(customer, repo, **extra):
    return client.post("/v1/account/imports", headers=site(customer),
                       json={"url": f"https://github.com/cognex-fixtures/{repo}", **extra})


def run_worker():
    """Process every queued import (what the background worker does)."""
    while (row := jobs.claim(uuid.uuid4())) is not None:
        jobs.process(row)


def status_of(customer, import_id):
    return client.get(f"/v1/account/imports/{import_id}", headers=site(customer)).json()


def execute(key, tasks):
    return client.post("/v1/execute", headers={"X-API-Key": key}, json={"tasks": tasks})


# -- the happy path: URL in, Ready out, copy the request, get a real result -----------------------------
def test_submit_only_the_url_becomes_ready_and_the_copied_request_works():
    c = new_customer()
    key = key_for(c, credits=5)
    r = submit(c, "geodist")                                   # the only input is the URL
    assert r.status_code == 202 and r.json()["status"] == "queued"
    import_id = r.json()["id"]
    assert r.json()["commit"] == fake_commit(github.parse_url("https://github.com/cognex-fixtures/geodist"))
    run_worker()
    imp = status_of(c, import_id)
    assert imp["status"] == "ready" and imp["stage"] == "ready", imp
    assert imp["operation"] == "geodist.geodist"
    assert imp["license"]["spdx"] == "MIT" and "Cognex Test Fixtures" in imp["license"]["copyright"]
    assert imp["example_response"]["results"][0]["status"] == "ok"   # the verification call's real result
    assert imp["example_response"]["results"][0]["id"] == imp["example_request"]["body"]["tasks"][0]["id"] == "1"
    ex = imp["example_request"]
    assert ex["url"] == "https://cognex.example/v1/execute" and "X-API-Key" in ex["headers"]
    assert ex["body"] == {"tasks": [{"id": "1", "operation": "geodist.geodist",
                                     "inputs": {"point1": [45.7597, 4.8422], "point2": [48.8567, 2.3508]}}]}
    assert "geodist.geodist" in ex["curl"] and "$COGNEX_API_KEY" in ex["curl"]
    assert balance(c) == 5                                     # importing and verifying cost nothing

    res = execute(key, ex["body"]["tasks"]).json()["results"][0]  # the customer's own, existing key
    assert res["status"] == "ok" and res["operation"] == "geodist.geodist"
    assert abs(res["output"]["result"] - 392.2172595594006) < 1e-6
    assert balance(c) == 4                                     # normal credit rule: 1 per run

    res = execute(key, [{"id": "2", "operation": "geodist.geodist",
                         "inputs": {"point1": [45.7597, 4.8422], "point2": [48.8567, 2.3508], "unit": "mi"}}]).json()
    assert abs(res["results"][0]["output"]["result"] - 243.7125060953981) < 1e-3
    res = execute(key, [{"id": "3", "request": "distance between two points on Earth",
                         "data": {"point1": [0, 0], "point2": [0, 1]}}]).json()["results"][0]
    assert res["status"] == "ok" and res["operation"] == "geodist.geodist"   # plain English reaches it too
    ops = client.get("/v1/operations", headers={"X-API-Key": key}).json()["operations"]
    assert any(o["operation"] == "geodist.geodist" and o.get("private") for o in ops)
    usage = client.get("/v1/account/usage", headers=site(c)).json()
    assert {"geodist.geodist"} <= {o["operation"] for o in usage["by_operation"]}
    keys = client.get("/v1/account/keys", headers=site(c)).json()["keys"]
    assert len(keys) == 1                                      # the verification key is never listed


def test_duplicates_return_the_existing_import():
    c = new_customer()
    first = submit(c, "geodist")
    again = submit(c, "geodist")
    assert first.status_code == 202 and again.status_code == 200
    assert again.json()["id"] == first.json()["id"]
    assert len(client.get("/v1/account/imports", headers=site(c)).json()["imports"]) == 1
    other = submit(new_customer(), "geodist")                  # another customer gets its own
    assert other.status_code == 202 and other.json()["id"] != first.json()["id"]
    run_worker()


# -- privacy ---------------------------------------------------------------------------------------------
def test_another_account_cannot_view_or_execute_the_import():
    a, b = new_customer(), new_customer()
    key_a, key_b = key_for(a), key_for(b)
    import_id = submit(a, "geodist").json()["id"]
    run_worker()
    assert status_of(a, import_id)["status"] == "ready"
    assert client.get(f"/v1/account/imports/{import_id}", headers=site(b)).status_code == 404
    assert client.get("/v1/account/imports", headers=site(b)).json()["imports"] == []
    inputs = {"point1": [45.7597, 4.8422], "point2": [48.8567, 2.3508]}
    res = execute(key_b, [{"id": "1", "operation": "geodist.geodist", "inputs": inputs}]).json()["results"][0]
    assert res["status"] == "error" and res["error_type"] == "unknown_operation"
    res = execute(key_b, [{"id": "2", "request": "distance between two points on Earth", "data": inputs}]).json()
    assert res["results"][0].get("operation") != "geodist.geodist"
    assert balance(b) == 10                                    # nothing ran, nothing charged
    ops_b = client.get("/v1/operations", headers={"X-API-Key": key_b}).json()["operations"]
    assert "geodist.geodist" not in {o["operation"] for o in ops_b}
    res = client.post("/v1/execute", headers={"X-API-Key": OPERATOR_KEY},
                      json={"tasks": [{"id": "1", "operation": "geodist.geodist", "inputs": inputs}]}).json()
    assert res["results"][0]["error_type"] == "unknown_operation"   # not even the operator key
    assert execute(key_a, [{"id": "1", "operation": "geodist.geodist", "inputs": inputs}]).json()[
        "results"][0]["status"] == "ok"


def test_import_check_keys_are_scoped_unbilled_and_unlisted():
    c = new_customer()
    key_for(c, credits=0)
    import_id = submit(c, "geodist").json()["id"]
    run_worker()
    k = accounts.create_key(c, "check", kind="import_check", scope=import_id)
    r = execute(k["secret"], [{"id": "1", "operation": "text.similarity", "inputs": {"a": "x", "b": "y"}}])
    assert r.status_code == 403
    r = execute(k["secret"], [{"id": "1", "operation": "geodist.geodist",
                               "inputs": {"point1": [0, 0], "point2": [0, 1]}}])
    assert r.status_code == 200 and r.json()["results"][0]["status"] == "ok"   # balance 0, still works
    assert [x["name"] for x in client.get("/v1/account/keys", headers=site(c)).json()["keys"]] == ["app"]


# -- licenses never block: recorded for attribution only ---------------------------------------------
def test_agpl_repo_is_no_longer_blocked_and_reaches_the_build_step(monkeypatch):
    stages = []
    real_update = jobs._update
    def record(import_id, **fields):
        if "stage" in fields:
            stages.append(fields["stage"])
        return real_update(import_id, **fields)
    monkeypatch.setattr(jobs, "_update", record)
    c = new_customer()
    key = key_for(c, credits=2)
    import_id = submit(c, "agpl").json()["id"]
    run_worker()
    imp = status_of(c, import_id)
    assert stages == ["fetching", "reading_license", "analyzing", "installing", "discovering", "registering",
                      "verifying", "ready"], stages            # past the license, through the build step
    assert imp["status"] == "ready" and "failure" not in imp, imp
    assert imp["license"]["spdx"] == "AGPL-3.0"                # attribution kept
    assert "Cognex Test Fixtures (AGPL fixture)" in imp["license"]["copyright"]
    assert (jobs.imports_dir() / import_id / "LICENSE").read_text().startswith(" " * 20 + "GNU AFFERO")
    res = execute(key, [{"id": "1", "operation": "agpl.agpl", "inputs": {"x": 3, "y": 4}}]).json()["results"][0]
    assert res["status"] == "ok" and res["output"] == {"result": 13}


def test_repo_without_a_license_file_imports_and_publishes():
    c = new_customer()
    key = key_for(c, credits=1)
    import_id = submit(c, "nolicense").json()["id"]
    run_worker()
    imp = status_of(c, import_id)
    assert imp["status"] == "ready" and imp["license"]["spdx"] == "NOASSERTION", imp
    assert execute(key, [{"id": "1", "operation": "nolicense.nolicense", "inputs": {"x": 2, "y": 5}}]).json()[
        "results"][0]["output"] == {"result": 11}
    # the operator can publish AGPL and unlicensed repos for every customer; attribution is listed as found
    for repo, spdx in (("agpl", "AGPL-3.0"), ("nolicense", "NOASSERTION")):
        name = f"math.score_{repo}_{uuid.uuid4().hex[:6]}"
        r = admin("POST", "/v1/admin/imports", json={"url": f"https://github.com/cognex-fixtures/{repo}",
                                                     "publish": {"name": name}})
        assert r.status_code in (200, 202), r.text
        run_worker()
        assert admin("GET", f"/v1/admin/imports/{r.json()['id']}").json()["published"]["live"]
        assert any(p["operation"] == name and p["license"] == spdx
                   for p in client.get("/v1/open-source").json()["projects"])
        admin("POST", f"/v1/admin/imports/{r.json()['id']}/unpublish")


def test_no_license_failure_codes_or_checks_remain():
    assert not {c for c in jobs.FAILURE_CODES if "licen" in c}
    from app.imports import licensing
    assert not hasattr(licensing, "verdict")
    assert all(len(entry) == 2 for entry in licensing.LICENSES)   # SPDX id + phrase: no allow/deny flag


# -- failures: a specific reason, never callable ---------------------------------------------------------
@pytest.mark.parametrize("repo, code, words", [
    ("ambiguous", "multiple_entry_points", "mean_of(), median_of()"),
    ("compiled", "unsupported_build", "compiled/_fast.c"),
    ("failing", "test_failed", "documentation says 3"),
    ("noreturn", "unsupported_output", "returned nothing"),
])
def test_failed_imports_say_why_and_are_never_callable(repo, code, words):
    c = new_customer()
    key = key_for(c)
    import_id = submit(c, repo).json()["id"]
    run_worker()
    imp = status_of(c, import_id)
    assert imp["status"] == "failed" and imp["failure"]["code"] == code, imp
    assert words in imp["failure"]["reason"]
    assert "operation" not in imp and "example_request" not in imp
    res = execute(key, [{"id": "1", "operation": f"{repo}.{repo}", "inputs": {"text": "x"}}]).json()["results"][0]
    assert res["error_type"] == "unknown_operation"
    assert balance(c) == 10


def test_ambiguous_repo_can_be_retried_with_an_entry_point():
    c = new_customer()
    key = key_for(c)
    import_id = submit(c, "ambiguous").json()["id"]
    run_worker()
    failed = status_of(c, import_id)
    assert failed["failure"]["details"]["candidates"] == ["mean_of", "median_of"]
    r = submit(c, "ambiguous", entry_point="median_of")
    assert r.status_code == 202 and r.json()["id"] == import_id
    run_worker()
    imp = status_of(c, import_id)
    assert imp["status"] == "ready" and imp["operation"] == "ambiguous.median_of"
    res = execute(key, [{"id": "1", "operation": "ambiguous.median_of", "inputs": {"values": [5, 1, 3, 2]}}]).json()
    assert res["results"][0]["output"] == {"result": 2.5}


def test_failed_verification_never_becomes_callable(monkeypatch):
    c = new_customer()
    key = key_for(c)
    monkeypatch.setattr(jobs, "VERIFY", lambda *a: (200, {"results": [{"status": "error", "error": "boom"}]}))
    import_id = submit(c, "typed").json()["id"]
    run_worker()
    imp = status_of(c, import_id)
    assert imp["status"] == "failed" and imp["failure"]["code"] == "verification_failed"
    res = execute(key, [{"id": "1", "operation": "typed.typed", "inputs": {"text": "x"}}]).json()["results"][0]
    assert res["error_type"] == "unknown_operation"


def test_typed_repo_gets_a_schema_and_inputs_are_validated_before_running():
    c = new_customer()
    key = key_for(c, credits=3)
    import_id = submit(c, "typed").json()["id"]
    run_worker()
    imp = status_of(c, import_id)
    assert imp["status"] == "ready"
    assert imp["input_schema"]["properties"]["case"]["enum"] == ["upper", "lower", "keep"]
    res = execute(key, [{"id": "1", "operation": "typed.typed", "inputs": {"text": "ab", "case": "upper"}}]).json()
    assert res["results"][0]["output"] == {"result": {"text": "AB-AB", "length": 5}}
    res = execute(key, [{"id": "2", "operation": "typed.typed", "inputs": {"text": 5}}]).json()["results"][0]
    assert res["error_type"] == "invalid_input" and balance(c) == 2   # rejected before running: free


def test_invalid_urls_are_rejected_at_submit():
    c = new_customer()
    for url in ("https://gitlab.com/a/b", "https://github.com/a", "file:///etc/passwd", "https://github.com/a/b?x=1"):
        r = client.post("/v1/account/imports", headers=site(c), json={"url": url})
        assert r.status_code == 400 and r.json()["error"] == "invalid_url", url
    r = client.post("/v1/account/imports", json={"url": "https://github.com/a/b"})
    assert r.status_code == 401                                # website-backend auth required


def test_ready_import_rebuilds_after_its_files_are_wiped(tmp_path):
    c = new_customer()
    key = key_for(c)
    import_id = submit(c, "geodist").json()["id"]
    run_worker()
    shutil.rmtree(jobs.imports_dir() / import_id)              # e.g. a redeploy without a volume
    res = execute(key, [{"id": "1", "operation": "geodist.geodist",
                         "inputs": {"point1": [0, 0], "point2": [0, 1]}}]).json()["results"][0]
    assert res["status"] == "ok" and abs(res["output"]["result"] - 111.195) < 0.01


# -- operator imports published for every customer -------------------------------------------------------
def admin(method, path, **kw):
    return client.request(method, path, headers={"X-API-Key": OPERATOR_KEY}, **kw)


def test_published_operation_is_callable_by_every_customer_with_their_own_key():
    name = "geo.distance_" + uuid.uuid4().hex[:6]
    before = client.get("/v1/operations").json()["catalog_version"]
    r = admin("POST", "/v1/admin/imports", json={"url": "https://github.com/cognex-fixtures/geodist", "publish": {
        "name": name, "summary": "Great-circle distance between two latitude/longitude points",
        "when_to_use": "Use when you need the distance between two places given as latitude/longitude pairs."}})
    assert r.status_code in (200, 202), r.text
    assert r.json()["published"]["operation"] == name and not r.json()["published"]["live"]   # live once Ready
    import_id = r.json()["id"]
    run_worker()
    imp = admin("GET", f"/v1/admin/imports/{import_id}").json()
    assert imp["status"] == "ready" and imp["published"]["operation"] == name and imp["published"]["live"], imp

    a, b = new_customer(), new_customer()
    key_a, key_b = key_for(a, credits=5), key_for(b, credits=5)
    lyon, paris = [45.7597, 4.8422], [48.8567, 2.3508]
    ra = execute(key_a, [{"id": "a", "operation": name, "inputs": {"point1": lyon, "point2": paris}}]).json()["results"][0]
    rb = execute(key_b, [{"id": "b", "operation": name, "inputs": {"point1": lyon, "point2": paris, "unit": "mi"},
                          "timeout_s": 20}]).json()["results"][0]          # its own inputs and options
    assert ra["status"] == "ok" and ra["operation"] == name and abs(ra["output"]["result"] - 392.2172595594006) < 1e-6
    assert rb["status"] == "ok" and rb["operation"] == name and abs(rb["output"]["result"] - 243.7125060953981) < 1e-3
    assert balance(a) == 4 and balance(b) == 4                 # each pays 1 credit from its own balance
    rp = execute(key_a, [{"id": "p", "request": "great-circle distance between two latitude/longitude points",
                          "data": {"point1": [0, 0], "point2": [0, 1]}}]).json()["results"][0]
    assert rp["status"] == "ok" and rp["operation"] == name    # plain English routes to it too
    bad = execute(key_b, [{"id": "x", "operation": name, "inputs": {"point1": "nope"}}]).json()["results"][0]
    assert bad["error_type"] == "invalid_input" and balance(b) == 4   # validated before running: free

    for key in (key_a, key_b, None):                           # in every catalog, including the public one
        ops = client.get("/v1/operations", headers={"X-API-Key": key} if key else {}).json()["operations"]
        entry = next(o for o in ops if o["operation"] == name)
        assert not entry.get("private") and entry["summary"].startswith("Great-circle")
    # discovery for AI app builders: current as soon as it is published, with a working example
    disc = client.get("/v1/operations").json()
    assert disc["catalog_version"] != before
    d = client.get(f"/v1/operations/{name}").json()
    assert d["purpose"].startswith("Great-circle") and d["when_to_use"].startswith("Use when you need the distance")
    assert [f["name"] for f in d["required_inputs"]] == ["point1", "point2"]
    unit = next(f for f in d["optional_settings"] if f["name"] == "unit")
    assert unit["default"] == "km" and "mi" in unit["allowed"]
    assert "geodist" not in json.dumps(d).replace(d["request_example"]["url"], "")   # no GitHub/library name
    ran = execute(key_b, d["request_example"]["body"]["tasks"]).json()["results"][0]
    assert ran["status"] == "ok" and ran["output"] == d["response_example"]["results"][0]["output"]
    assert any(p["operation"] == name and p["license"] == "MIT" for p in
               client.get("/v1/open-source").json()["projects"])  # attribution kept
    assert name in {o["operation"] for o in client.get("/v1/account/usage", headers=site(a)).json()["by_operation"]}
    assert admin("POST", "/v1/execute", json={"tasks": [{"id": "o", "operation": name, "inputs": {
        "point1": lyon, "point2": paris}}]}).json()["results"][0]["status"] == "ok"

    assert admin("POST", f"/v1/admin/imports/{import_id}/unpublish").json().get("published") is None
    gone = execute(key_a, [{"id": "g", "operation": name, "inputs": {"point1": lyon, "point2": paris}}]).json()
    assert gone["results"][0]["error_type"] == "unknown_operation" and balance(a) == 3
    assert name not in {o["operation"] for o in client.get("/v1/operations").json()["operations"]}
    assert client.get(f"/v1/operations/{name}").status_code == 404


def test_publish_rules():
    c = new_customer()
    key_for(c, credits=0)
    customer_import = submit(c, "typed").json()["id"]
    r = admin("POST", "/v1/admin/imports", json={"url": "https://github.com/cognex-fixtures/typed"})
    own = r.json()["id"]
    run_worker()
    # a customer's import stays private: the operator can't see or publish it
    assert admin("GET", f"/v1/admin/imports/{customer_import}").status_code == 404
    assert admin("POST", f"/v1/admin/imports/{customer_import}/publish",
                 json={"name": "text.decorate_x"}).json()["error"] == "not_publishable"
    assert admin("POST", f"/v1/admin/imports/{own}/publish", json={"name": "Bad Name"}).json()["error"] == "invalid_name"
    assert admin("POST", f"/v1/admin/imports/{own}/publish",
                 json={"name": "text.similarity"}).json()["error"] == "name_taken"     # a built-in
    name = "text.decorate_" + uuid.uuid4().hex[:6]
    r = admin("POST", f"/v1/admin/imports/{own}/publish", json={"name": name})   # already ready: live at once
    assert r.status_code == 200 and r.json()["published"]["live"], r.text
    other = admin("POST", "/v1/admin/imports", json={"url": "https://github.com/cognex-fixtures/ambiguous",
                                                     "entry_point": "median_of"}).json()["id"]
    assert admin("POST", f"/v1/admin/imports/{other}/publish", json={"name": name}).json()["error"] == "name_taken"
    run_worker()
    key = key_for(new_customer(), credits=1)
    res = execute(key, [{"id": "1", "operation": name, "inputs": {"text": "ab", "case": "upper"}}]).json()["results"][0]
    assert res["status"] == "ok" and res["output"] == {"result": {"text": "AB-AB", "length": 5}}
    # admin endpoints need the operator key; customers' keys and the website secret don't work
    for headers in ({"X-API-Key": key}, site(c), {}):
        assert client.post("/v1/admin/imports", headers=headers,
                           json={"url": "https://github.com/cognex-fixtures/geodist"}).status_code in (401, 403)
