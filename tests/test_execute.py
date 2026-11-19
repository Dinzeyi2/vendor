import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
KEY = "test-key"
HEADERS = {"X-API-Key": KEY}


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("API_KEY", KEY)


def test_health_is_public():
    assert client.get("/health").json() == {"status": "ok"}


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
