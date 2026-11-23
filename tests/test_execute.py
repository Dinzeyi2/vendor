import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
KEY = "test-key"
HEADERS = {"X-API-Key": KEY}

# Minimal valid inputs (inputs are schema-checked before anything runs)
LP = {"c": [1], "A": [[1]], "b": [1], "cone": {"l": 1}}
XG = {"X": [[1]], "y": [1], "X_predict": [[1]]}
PH = {"bodies": [{"shape": {"type": "ball", "radius": 1}}]}


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("API_KEY", KEY)


def test_health_is_public():
    body = client.get("/health").json()
    assert body["status"] == "ok" and "commit" in body


def test_rapidfuzz_compare():
    r = client.post("/v1/execute", headers=HEADERS, json={"tasks": [
        {"id": "t1", "operation": "rapidfuzz.compare", "inputs": {"a": "Drift Protein", "b": "Drift Proteins"}},
    ]})
    assert r.status_code == 200
    res = r.json()["results"][0]
    assert res["status"] == "ok"
    assert 0 <= res["output"]["score"] <= 100
    assert round(res["output"]["score"], 2) == 96.30


def test_identical_and_errors():
    r = client.post("/v1/execute", headers=HEADERS, json={"tasks": [
        {"id": "same", "operation": "rapidfuzz.compare", "inputs": {"a": "x", "b": "x"}},
        {"id": "missing", "operation": "rapidfuzz.compare", "inputs": {"a": "x"}},
        {"id": "unknown", "operation": "nope", "inputs": {}},
    ]})
    out = {t["id"]: t for t in r.json()["results"]}
    assert out["same"]["output"]["score"] == 100
    assert out["missing"]["status"] == "error"
    assert out["unknown"]["status"] == "error"


def test_empty_tasks_rejected():
    assert client.post("/v1/execute", headers=HEADERS, json={"tasks": []}).status_code == 422


BODY = {"tasks": [{"id": "t", "operation": "rapidfuzz.compare", "inputs": {"a": "a", "b": "b"}}]}


def test_missing_key_rejected():
    assert client.post("/v1/execute", json=BODY).status_code == 401


def test_wrong_key_rejected():
    assert client.post("/v1/execute", headers={"X-API-Key": "wrong"}, json=BODY).status_code == 401


def test_unconfigured_server_fails_closed(monkeypatch):
    monkeypatch.delenv("API_KEY")
    assert client.post("/v1/execute", headers=HEADERS, json=BODY).status_code == 503


# -- Cognex names and compatibility aliases ------------------------------------------------------
from app import catalog  # noqa: E402
from app.registry import OPERATIONS, WORKERS  # noqa: E402

PUBLIC_NAMES = {"text.similarity", "vectors.nearest_neighbors", "tabular.train_and_predict",
                "optimization.solve_convex", "causal.estimate_effect", "image.detect_edges",
                "image.extract_text", "math.discover_equation", "scheduling.school_timetable",
                "physics.simulate_rigid_bodies", "text.rank_matches"}
OLD_NAMES = {"rapidfuzz.compare", "hnswlib.knn_query", "xgboost.train_predict", "scs.solve",
             "dowhy.estimate_effect", "kornia.canny", "paddleocr.ocr", "pysr.symbolic_regression",
             "timefold.school_timetabling", "rapier.simulate"}


def test_every_operation_has_a_public_name_and_an_alias():
    assert set(OPERATIONS) == PUBLIC_NAMES == set(catalog.BY_NAME)
    assert set(catalog.ALIASES) == OLD_NAMES
    assert set(catalog.ALIASES.values()) == PUBLIC_NAMES - {"text.rank_matches"}  # new: no old name
    assert all(OPERATIONS[catalog.ALIASES[old]].internal == old for old in OLD_NAMES)


def test_public_name_and_alias_give_the_same_result_and_echo_the_name_sent():
    r = client.post("/v1/execute", headers=HEADERS, json={"tasks": [
        {"id": "new", "operation": "text.similarity", "inputs": {"a": "Drift Protein", "b": "Drift Proteins"}},
        {"id": "old", "operation": "rapidfuzz.compare", "inputs": {"a": "Drift Protein", "b": "Drift Proteins"}},
    ]})
    new, old = r.json()["results"]
    assert new["operation"] == "text.similarity" and old["operation"] == "rapidfuzz.compare"
    assert new["status"] == old["status"] == "ok" and new["output"] == old["output"]


def test_workers_receive_their_internal_name_for_both_spellings(monkeypatch):
    seen = []
    monkeypatch.setattr(WORKERS["scs"], "call",
                        lambda op, inputs, timeout, on_start=None: seen.append(op) or {"status": "solved"})
    r = client.post("/v1/execute", headers=HEADERS, json={"tasks": [
        {"id": "a", "operation": "optimization.solve_convex", "inputs": LP},
        {"id": "b", "operation": "scs.solve", "inputs": LP}]})
    assert [t["status"] for t in r.json()["results"]] == ["ok", "ok"]
    assert seen == ["scs.solve", "scs.solve"]


def test_unknown_operation_points_to_the_catalog():
    r = client.post("/v1/execute", headers=HEADERS, json={"tasks": [{"id": "x", "operation": "text.nope"}]})
    res = r.json()["results"][0]
    assert res["error_type"] == "unknown_operation" and "GET /v1/operations" in res["error"]


def test_public_catalog_lists_only_cognex_names_with_cost_and_license():
    r = client.get("/v1/operations")                      # no key needed
    assert r.status_code == 200
    body = r.json()
    ops = body["operations"]
    assert body["credits_per_operation"] == 1
    assert {o["operation"] for o in ops} == PUBLIC_NAMES
    text = r.text
    assert not any(old in text for old in OLD_NAMES)      # aliases are never advertised publicly
    for o in ops:
        assert o["credits"] == 1 and o["summary"]
        assert set(o) == {"operation", "summary", "credits", "default_timeout_s", "max_timeout_s", "input_schema", "example",
                          "purpose", "when_to_use", "required_inputs", "optional_settings", "request_example",
                          "response_example", "plain_english_example"}  # original fields kept, discovery added
    projects = ["rapidfuzz", "hnswlib", "xgboost", "dowhy", "kornia", "paddle", "pysr", "timefold",
                "rapier", "github", "scs"]
    assert not [p for p in projects if p in text.lower()]  # no library names in the catalog
    assert body["open_source_acknowledgements"] == "/v1/open-source"


def test_open_source_page_lists_every_project_and_license():
    r = client.get("/v1/open-source")                      # public, separate from the catalog
    assert r.status_code == 200
    projects = r.json()["projects"]
    assert {p["operation"] for p in projects} == PUBLIC_NAMES
    assert all(p["license"] in {"MIT", "Apache-2.0"} and p["copyright"] and p["source"].startswith("https://")
               for p in projects)


def test_engine_fields_are_dropped_for_public_names_only(monkeypatch):
    monkeypatch.setattr(WORKERS["rapier"], "call", lambda op, inputs, timeout, on_start=None:
                        {"bodies": [], "rapier_version": "0.14.0"})
    r = client.post("/v1/execute", headers=HEADERS, json={"tasks": [
        {"id": "new", "operation": "physics.simulate_rigid_bodies", "inputs": PH},
        {"id": "old", "operation": "rapier.simulate", "inputs": PH}]})
    new, old = r.json()["results"]
    assert new["output"] == {"bodies": []}
    assert old["output"] == {"bodies": [], "rapier_version": "0.14.0"}   # unchanged for existing integrations


def test_operator_catalog_shows_aliases_and_bad_keys_are_rejected():
    ops = client.get("/v1/operations", headers=HEADERS).json()["operations"]
    by = {o["operation"]: o for o in ops}
    assert by["text.similarity"]["aliases"] == ["rapidfuzz.compare"] and "runs_in" in by["text.similarity"]
    assert client.get("/v1/operations", headers={"X-API-Key": "wrong"}).status_code == 401


def test_customer_errors_use_the_public_name(monkeypatch):
    from app import main
    from app.workers import WorkerFailure

    def timeout(op, inputs, timeout, on_start=None):
        raise WorkerFailure("timeout", f"worker 'scs' timed out while running {op}", started=True)

    monkeypatch.setattr(WORKERS["scs"], "call", timeout)
    task = main.Task(id="t", operation="optimization.solve_convex", inputs=LP)
    import asyncio
    res = asyncio.run(main._run(main._plan(task), diagnostics=False))
    assert res.error_type == "timeout" and "optimization.solve_convex" in res.error and "scs.solve" not in res.error
