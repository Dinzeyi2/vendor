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
from app.imports import github, jobs, private, runtimes
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
    shutil.copytree(FIXTURES / ref.repo.split("--")[0], dest)   # "name--variant": same files, another repo
    return dest


def verify_in_process(customer_id, import_id, operation, inputs):
    """The same check as production (a real POST /v1/execute with a scoped key), via the test client."""
    key = accounts.create_key(customer_id, "Import check (automatic)", kind="import_check", scope=import_id)
    try:
        r = client.post("/v1/execute", headers={"X-API-Key": key["secret"]},
                        json={"tasks": [{"id": "import-check", "operation": operation, "inputs": inputs, "async": False}]})
        return r.status_code, r.json()
    finally:
        accounts.revoke_key(customer_id, uuid.UUID(key["id"]))


@pytest.fixture(scope="module")
def build_cache(tmp_path_factory):
    return tmp_path_factory.mktemp("builds")   # shared by the module's tests: same repo + commit = reused build


@pytest.fixture(autouse=True)
def env(monkeypatch, database_url, tmp_path, build_cache):
    db.close()
    for k, v in {"DATABASE_URL": database_url, "WEBSITE_BACKEND_SECRET": SERVER_SECRET, "API_KEY": OPERATOR_KEY,
                 "CREDITS_GRANT_SECRET": GRANT_SECRET, "BILLING_MODE": "on", "COGNEX_SANDBOX": "insecure-dev",
                 "IMPORTS_DIR": str(tmp_path / "imports"), "COGNEX_PUBLIC_URL": "https://cognex.example",
                 "COGNEX_BUILDS_DIR": str(build_cache)}.items():
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
    assert stages == ["fetching", "reading_license", "analyzing", "building", "discovering", "registering",
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
    assert not any(isinstance(x, bool) for t in licensing.TITLES for x in t[:2])   # no allow/deny flag


def test_import_records_the_projects_attribution_and_the_operator_can_refresh_it():
    c = new_customer()
    key_for(c, credits=0)
    import_id = submit(c, "gplreadme").json()["id"]
    run_worker()
    imp = status_of(c, import_id)
    assert imp["status"] == "ready", imp
    want = {"spdx": "GPL-2.0-or-later", "copyright": "Copyright 2001, Ada Example <ada@example.org>, all rights "
                                                      "reserved.; Copyright 2026, Tomaž Example <tomaz@example.org>"}
    assert imp["license"] == want
    # an import recorded by the old detector (wrong label + template copyright), files gone after a redeploy
    row = jobs.get_any(uuid.UUID(import_id))
    bad = {**row["manifest"], "built_on": {**row["manifest"]["built_on"], "license": "LGPL",
                                           "copyright": "Copyright (C) <year>  <name of author>"}}
    jobs._update(import_id, license_spdx="LGPL", manifest=bad)
    shutil.rmtree(jobs.imports_dir() / import_id)
    assert status_of(c, import_id)["license"]["spdx"] == "LGPL"
    assert client.post(f"/v1/admin/imports/{import_id}/refresh-attribution",
                       headers=site(c)).status_code in (401, 403)        # operator only
    r = admin("POST", f"/v1/admin/imports/{import_id}/refresh-attribution")
    assert r.status_code == 200, r.text
    assert r.json()["before"] == {"license": "LGPL", "copyright": "Copyright (C) <year>  <name of author>"}
    assert r.json()["after"] == {"license": want["spdx"], "copyright": want["copyright"]}
    imp = status_of(c, import_id)
    assert imp["license"] == want and imp["status"] == "ready"           # nothing else changed


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
    build_key = jobs.get_any(uuid.UUID(import_id))["build_key"]
    shutil.rmtree(jobs.imports_dir() / import_id, ignore_errors=True)   # e.g. a redeploy without a volume
    shutil.rmtree(runtimes.builds_dir() / build_key)                    # ...and the build cache is gone too
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


# -- the general importer: every runtime through the same pipeline ------------------------------------
from app.config import node_bin, npm_cli, tool  # noqa: E402


def ready(customer, repo, **extra):
    r = submit(customer, repo, **extra)
    assert r.status_code in (200, 202), r.text
    run_worker()
    return status_of(customer, r.json()["id"])


def call(key, op, inputs, **task):
    return execute(key, [{"id": "1", "operation": op, "inputs": inputs, **task}]).json()["results"][0]


def test_python_package_is_built_before_import_so_generated_modules_exist():
    c = new_customer()
    key = key_for(c, credits=2)
    imp = ready(c, "genversion")            # importing the source tree fails: _version.py is made by the build
    assert imp["status"] == "ready" and imp["runtime"] == "python", imp
    assert call(key, imp["operation"], {"x": 21})["output"] == {"result": 42}


def test_python_command_line_tool_becomes_a_command_operation():
    c = new_customer()
    key = key_for(c, credits=2)
    imp = ready(c, "pycli")
    assert imp["status"] == "ready" and imp["operation"] == "pycli.pycli", imp
    assert imp["example_response"]["results"][0]["output"]["stdout"].strip() == "HELLO WORLD"
    res = call(key, "pycli.pycli", {"args": [], "stdin": "stressed"})
    assert res["status"] == "ok" and res["output"]["stdout"].strip() == "desserts"


def test_class_only_library_says_what_is_missing_then_a_saved_configuration_fixes_it():
    c = new_customer()
    key = key_for(c, credits=2)
    imp = ready(c, "shapes")
    assert imp["status"] == "failed" and imp["failure"]["code"] == "no_callable_function", imp
    assert imp["failure"]["missing"][0]["field"] == "operation"            # exactly what a configuration must add
    assert imp["failure"]["config_template"]["operation"]["kind"] == "function"
    config = {"operation": {
        "kind": "function", "name": "area", "summary": "Area of a polygon given its corner points.",
        "target": "shapes:Polygon", "args": {"points": "{points}"}, "then": [{"attr": "area"}],
        "input_schema": {"type": "object", "required": ["points"], "properties": {
            "points": {"type": "array", "items": {"type": "array", "items": {"type": "number"}}}}},
        "sample_inputs": {"points": [[0, 0], [4, 0], [4, 3]]}, "expected_output": 6.0}}
    assert admin("PUT", "/v1/admin/repo-configs/cognex-fixtures/shapes", json={"runtime": "nodejs"}).status_code == 422
    r = admin("PUT", "/v1/admin/repo-configs/cognex-fixtures/shapes", json=config)
    assert r.status_code == 200 and imp["id"] in r.json()["requeued_imports"], r.text
    run_worker()
    imp = status_of(c, imp["id"])
    assert imp["status"] == "ready" and imp["operation"] == "shapes.area", imp
    assert call(key, "shapes.area", {"points": [[0, 0], [2, 0], [2, 2], [0, 2]]})["output"] == {"result": 4.0}
    other = new_customer()                   # the saved configuration is reused by later imports
    key2 = key_for(other, credits=1)
    assert ready(other, "shapes")["status"] == "ready"
    assert call(key2, "shapes.area", {"points": [[0, 0], [1, 0], [0, 1]]})["output"] == {"result": 0.5}
    assert admin("GET", "/v1/admin/repo-configs/cognex-fixtures/shapes").json()["config"] == config


@pytest.mark.skipif(not npm_cli(), reason="Node.js with npm is needed")
def test_node_library_is_discovered_from_its_readme_examples():
    c = new_customer()
    key = key_for(c, credits=2)
    imp = ready(c, "nodeslug")
    assert imp["status"] == "ready" and imp["runtime"] == "node", imp
    assert imp["example_response"]["results"][0]["output"] == {"result": "deja-vu-again"}
    assert imp["input_schema"]["required"] == ["text"]
    assert call(key, imp["operation"], {"text": "Hello Node World"})["output"] == {"result": "hello-node-world"}


@pytest.mark.skipif(not npm_cli(), reason="Node.js with npm is needed")
def test_node_command_line_tool():
    c = new_customer()
    key = key_for(c, credits=1)
    imp = ready(c, "nodecli")
    assert imp["status"] == "ready" and imp["operation"] == "nodecli.nodecli", imp
    res = call(key, "nodecli.nodecli", {"args": ["greet", "Grace"]})
    assert res["status"] == "ok" and res["output"]["stdout"].strip() == "Hello, Grace!"


def test_css_package_becomes_frontend_assets():
    c = new_customer()
    key = key_for(c, credits=2)
    imp = ready(c, "cssfw")
    assert imp["status"] == "ready" and imp["runtime"] == "frontend" and imp["operation"] == "cssfw.assets", imp
    listing = call(key, "cssfw.assets", {})["output"]["assets"]
    assert {a["path"] for a in listing} == {"dist/cssfw.css", "dist/cssfw.min.css"}
    css = call(key, "cssfw.assets", {"path": "dist/cssfw.css"})["output"]
    assert css["content_type"] == "text/css" and ".btn{" in css["content"]


@pytest.mark.skipif(not (tool("cmake") and (tool("cc") or tool("gcc"))), reason="a C toolchain and CMake are needed")
def test_native_cmake_program_becomes_a_command_operation():
    c = new_customer()
    key = key_for(c, credits=1)
    imp = ready(c, "cnative")
    assert imp["status"] == "ready" and imp["runtime"] == "native", imp
    assert call(key, imp["operation"], {"args": [], "stdin": "abc"})["output"]["stdout"] == "ABC"


@pytest.mark.parametrize("repo, runtime, words", [("dockeronly", "container", "container runtime"),
                                                   ("pixiapp", "conda", "conda/pixi")])
def test_projects_without_a_runtime_say_exactly_what_is_missing(repo, runtime, words):
    c = new_customer()
    imp = ready(c, repo)
    assert imp["status"] == "failed" and imp["failure"]["code"] == "runtime_not_available", imp
    assert imp["failure"]["runtime"] == runtime and words in imp["failure"]["reason"]
    assert imp["failure"]["missing"]


def test_the_same_repo_commit_and_configuration_reuse_one_build():
    a, b = new_customer(), new_customer()
    first, second = ready(a, "genversion"), ready(b, "genversion")
    rows = [jobs.get_any(uuid.UUID(i["id"])) for i in (first, second)]
    assert rows[0]["build_key"] == rows[1]["build_key"]
    assert (runtimes.builds_dir() / rows[0]["build_key"] / "build.json").is_file()
    assert len([p for p in runtimes.builds_dir().iterdir() if p.name == rows[0]["build_key"]]) == 1


def test_long_running_operations_return_a_job_id_and_the_result_comes_from_the_jobs_api():
    import time
    config = {"operation": {
        "kind": "function", "name": "double", "summary": "Double a number in a background job.",
        "target": "genversion:genversion", "args": {"x": "{x}"}, "long_running": True,
        "input_schema": {"type": "object", "required": ["x"], "properties": {"x": {"type": "number"}}},
        "sample_inputs": {"x": 2}, "expected_output": 4}}
    assert admin("PUT", "/v1/admin/repo-configs/cognex-fixtures/genversion--lr", json=config).status_code == 200
    c = new_customer()
    key = key_for(c, credits=3)
    imp = ready(c, "genversion--lr")
    assert imp["status"] == "ready", imp
    res = call(key, imp["operation"], {"x": 5})
    assert res["status"] == "queued" and res["job_id"], res
    deadline = time.time() + 60
    while time.time() < deadline:
        job = client.get(f"/v1/jobs/{res['job_id']}", headers={"X-API-Key": key}).json()
        if job["status"] in ("ok", "error"):
            break
        time.sleep(0.3)
    assert job["status"] == "ok" and job["output"] == {"result": 10} and job["credits_charged"] == 1, job
    assert balance(c) == 2
    assert call(key, imp["operation"], {"x": 1}, **{"async": False})["output"] == {"result": 2}   # direct on request
    other = key_for(new_customer(), credits=0)
    assert client.get(f"/v1/jobs/{res['job_id']}", headers={"X-API-Key": other}).status_code == 404
