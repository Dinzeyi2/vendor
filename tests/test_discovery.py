"""GET /v1/operations as discovery for apps and AI app builders (built-in operations, no database)."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from app import capability_checks, router
from app.main import app
from app.registry import CAPABILITIES

client = TestClient(app)


@pytest.fixture(autouse=True)
def no_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("API_KEY", "operator-test-key")


def catalog():
    r = client.get("/v1/operations")
    assert r.status_code == 200
    return r


def test_every_operation_says_what_it_is_for_and_how_to_call_it():
    body = catalog().json()
    assert body["count"] == len(body["operations"]) == len(CAPABILITIES)
    call = body["how_to_call"]
    assert call["method"] == "POST" and call["endpoint"].endswith("/v1/execute") and "X-API-Key" in call["headers"]
    for op in body["operations"]:
        cap = CAPABILITIES[op["operation"]]
        assert op["purpose"] == cap.summary and len(op["when_to_use"]) >= 10
        assert [f["name"] for f in op["required_inputs"]] == cap.input_schema["required"]
        assert all(f["type"] for f in op["required_inputs"] + op["optional_settings"])
        optional = {f["name"]: f for f in op["optional_settings"]}
        assert set(optional) == set(cap.input_schema["properties"]) - set(cap.input_schema["required"]) | {"timeout_s"}
        assert optional["timeout_s"]["default"] == cap.default_timeout and optional["timeout_s"]["maximum"] == cap.max_timeout
        req = op["request_example"]
        assert req["method"] == "POST" and req["url"].endswith("/v1/execute")
        task = req["body"]["tasks"][0]
        assert task["operation"] == op["operation"] and router.validation_error(cap, task["inputs"]) is None
        res = op["response_example"]["results"][0]
        assert res["status"] == "ok" and res["operation"] == op["operation"] and res["output"]
        assert not set(cap.engine_fields) & set(res["output"])
        pe = op["plain_english_example"]["tasks"][0]
        d = router.route(pe["request"], pe["data"], list(CAPABILITIES.values()))
        assert d.runnable and d.capability.name == op["operation"]


def test_discovery_never_names_libraries():
    text = json.dumps(catalog().json()["operations"]).lower()
    words = capability_checks.library_words(list(CAPABILITIES.values()))
    assert [w for w in words if re.search(rf"\b{re.escape(w)}\b", text)] == []


def test_request_examples_run_as_written():
    for name in ("text.similarity", "text.rank_matches"):  # the inline ones; scripts/discovery_test.py runs all
        op = client.get(f"/v1/operations/{name}").json()
        r = client.post("/v1/execute", headers={"X-API-Key": "operator-test-key"}, json=op["request_example"]["body"])
        got = r.json()["results"][0]
        assert got["status"] == "ok" and got["output"] == op["response_example"]["results"][0]["output"]


def test_version_etag_and_single_operation():
    r = catalog()
    ver = r.json()["catalog_version"]
    assert r.headers["etag"] == f'"{ver}"' and r.headers["cache-control"] == "no-cache"
    assert client.get("/v1/operations", headers={"If-None-Match": f'"{ver}"'}).status_code == 304
    one = client.get("/v1/operations/rapidfuzz.compare").json()        # an old name finds the new entry
    assert one["operation"] == "text.similarity" and "how_to_call" in one
    assert client.get("/v1/operations/nope.nothing").status_code == 404
