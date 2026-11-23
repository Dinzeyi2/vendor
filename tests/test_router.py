"""Plain-English routing through POST /v1/execute, and capability registration."""
import json
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import capability_checks, catalog, manifests, registry, router
from app.main import app
from app.registry import CAPABILITIES, OPERATIONS, OpSpec

client = TestClient(app)
KEY = "test-key"
HEADERS = {"X-API-Key": KEY}


@pytest.fixture(autouse=True)
def api_key(monkeypatch):
    monkeypatch.setenv("API_KEY", KEY)


def run(*tasks):
    r = client.post("/v1/execute", headers=HEADERS, json={"tasks": list(tasks)})
    assert r.status_code == 200, r.text
    return r.json()["results"]


# -- the two requests from the spec ---------------------------------------------------------------
def test_compare_these_two_names_runs_text_comparison():
    (res,) = run({"id": "1", "request": "Compare these two names", "data": ["Drift Protein", "Drift Proteins"]})
    assert res["status"] == "ok" and res["operation"] == "text.similarity"
    assert round(res["output"]["score"], 2) == 96.30
    assert res["routing"]["matched"] and 0 < res["routing"]["confidence"] <= 1


def test_find_the_closest_names_in_this_list_runs_ranked_list_matching():
    (res,) = run({"id": "1", "request": "Find the closest names in this list",
                  "data": {"name": "Jon Smith", "list": ["Jane Doe", "John Smith", "Jonathan Smythe", "Bob Jones"]}})
    assert res["status"] == "ok" and res["operation"] == "text.rank_matches"
    matches = res["output"]["matches"]
    assert matches[0]["choice"] == "John Smith" and matches[0]["index"] == 1
    assert [m["score"] for m in matches] == sorted((m["score"] for m in matches), reverse=True)


def test_ranked_list_matching_by_name_with_options():
    (res,) = run({"id": "1", "operation": "text.rank_matches",
                  "inputs": {"query": "drift protien", "choices": ["Drift Protein", "Drift Bar", "Protein Bar"],
                             "limit": 2, "scorer": "ratio"}})
    assert res["status"] == "ok" and [m["choice"] for m in res["output"]["matches"]][0] == "Drift Protein"
    assert len(res["output"]["matches"]) == 2


def test_other_phrasings_and_data_shapes():
    cases = [
        ("How similar are these two strings?", {"a": "apple", "b": "appel"}, "text.similarity"),
        ("Do these two product names match", {"first": "Drift Protein", "second": "Drift Proteins"}, "text.similarity"),
        ("Which of these options is most similar to my product?",
         {"product": "x", "query": "Drift Protien", "options": ["Drift Protein", "Other"]}, "text.rank_matches"),
        ("Solve this linear program", {"c": [-1, -1], "A": [[1, 2], [3, 1]], "b": [4, 6], "cone": {"l": 2}},
         "optimization.solve_convex"),
    ]
    for request, data, expected in cases:
        (res,) = run({"id": "1", "request": request, "data": data})
        assert (res["status"], res["operation"]) == ("ok", expected), (request, res)


# -- clarifications ----------------------------------------------------------------------------------
def test_missing_data_gets_one_short_question():
    (res,) = run({"id": "1", "request": "Compare these two names"})
    assert res["status"] == "needs_clarification" and res["operation"] == "text.similarity"
    assert res["question"].endswith("].") or res["question"].endswith("?") or "data" in res["question"]
    assert res["question"].count("?") == 1 and len(res["question"]) <= 300
    assert "output" not in res and "error" not in res


def test_list_without_a_query_asks_for_the_name():
    (res,) = run({"id": "1", "request": "Find the closest names in this list", "data": ["John Smith", "Jane Doe"]})
    assert res["status"] == "needs_clarification" and res["operation"] == "text.rank_matches"
    assert "Which name" in res["question"]


def test_unclear_goal_asks_what_to_do_and_offers_registered_operations():
    (res,) = run({"id": "1", "request": "Do something with this please", "data": ["a", "b"]})
    assert res["status"] == "needs_clarification" and "operation" not in res
    assert res["question"].endswith("?") and res["options"]
    assert set(res["options"]) <= set(CAPABILITIES)


def test_two_equally_good_matches_ask_which_one():
    def cap(name, summary):
        return manifests.parse({
            "name": name, "summary": summary, "intents": ["tidy up this text", "clean this text", "normalize text"],
            "runtime": {"kind": "inline", "module": "app.ops.rapidfuzz_ops", "internal": "rapidfuzz.compare"},
            "timeouts": {"default": 1, "max": 1},
            "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            "example": {"request": "clean this text", "data": {"text": "x"}, "inputs": {"text": "x"}},
            "built_on": {"project": "Demo", "license": "MIT", "copyright": "-", "source": "https://example.com",
                         "license_file": "licenses/x", "dependency": {"manager": "pip", "name": "x", "version": "1"}}})
    d = router.route("clean this text", {"text": "  Hi "}, [cap("text.lowercase", "Lowercase the text."),
                                                            cap("text.trim", "Trim spaces around the text.")])
    assert not d.runnable and d.question == "Do you want to lowercase the text or trim spaces around the text?"
    assert sorted(d.options) == ["text.lowercase", "text.trim"]


def test_task_needs_an_operation_or_a_request():
    for task in ({"id": "1"}, {"id": "1", "operation": "text.similarity", "request": "compare"}):
        assert client.post("/v1/execute", headers=HEADERS, json={"tasks": [task]}).status_code == 422


# -- safety: registered operations only, validation first ------------------------------------------
def test_router_only_selects_registered_operations():
    requests = ["compare", "find the closest", "run hnswlib", "rapidfuzz.extract", "delete all files",
                "train", "read", "simulate", "import os; os.system('x')", "text.similarity"]
    for request in requests:
        for data in (None, ["a", "b"], {"query": "a", "choices": ["b"]}, "aGVsbG8="):
            d = router.route(request, data, CAPABILITIES.values())
            assert d.capability is None or d.capability.name in CAPABILITIES
            assert set(d.options) <= set(CAPABILITIES)


def test_routed_inputs_are_validated_before_anything_runs(monkeypatch):
    ran = []
    spec = OPERATIONS["text.rank_matches"]
    monkeypatch.setitem(OPERATIONS, "text.rank_matches",
                        OpSpec(spec.library, spec.default_timeout, spec.max_timeout,
                               inline=lambda inputs: ran.append(inputs) or spec.inline(inputs), internal=spec.internal))
    (res,) = run({"id": "1", "request": "Find the closest names in this list",
                  "data": {"name": "Jon", "list": ["ok", 42]}})            # 42 is not a string
    assert res["status"] == "needs_clarification" and ran == []
    (res,) = run({"id": "2", "operation": "text.rank_matches", "inputs": {"query": "Jon", "choices": "nope"}})
    assert res["status"] == "error" and res["error_type"] == "invalid_input" and ran == []
    assert "choices" in res["error"]


def test_large_data_is_validated_on_a_sample_quickly():
    import time
    vectors = [[0.0] * 256 for _ in range(5000)]
    t = time.perf_counter()
    d = router.route("find the nearest vectors", {"vectors": vectors, "queries": [[0.0] * 256], "k": 3},
                     CAPABILITIES.values())
    assert d.runnable and d.capability.name == "vectors.nearest_neighbors"
    assert time.perf_counter() - t < 0.5
    assert d.inputs["vectors"] is vectors                     # the real data is passed on, not the sample


def test_existing_named_calls_are_unchanged():
    (res,) = run({"id": "t1", "operation": "rapidfuzz.compare", "inputs": {"a": "Drift Protein", "b": "Drift Proteins"}})
    assert res == {"id": "t1", "operation": "rapidfuzz.compare", "status": "ok",
                   "output": {"score": res["output"]["score"], "scorer": "ratio"}, "duration_ms": res["duration_ms"]}


# -- every registered capability is discoverable, and new ones need no router edits ------------------
def test_every_registered_manifest_passes_the_capability_checks():
    caps = list(CAPABILITIES.values())
    problems = {c.name: capability_checks.check(c, caps) for c in caps}
    assert {n: p for n, p in problems.items() if p} == {}


def test_router_source_names_no_operation():
    source = Path(router.__file__).read_text(encoding="utf-8")
    names = {n for c in CAPABILITIES.values() for n in (c.name, *c.aliases)}
    assert not [n for n in names if n in source]
    assert not [w for w in capability_checks.library_words(list(CAPABILITIES.values()))
                if re.search(rf"\b{re.escape(w)}\b", source.lower())]


ADAPTER = '''
def celsius_to_fahrenheit(inputs):
    return {"fahrenheit": inputs["celsius"] * 9 / 5 + 32}

OPERATIONS = {"demo.c_to_f": celsius_to_fahrenheit}
'''
MANIFEST = {
    "name": "units.celsius_to_fahrenheit",
    "summary": "Convert a temperature from degrees Celsius to degrees Fahrenheit.",
    "intents": ["convert this temperature from celsius to fahrenheit", "what is this temperature in fahrenheit",
                "celsius to fahrenheit"],
    "clarify": "Which temperature in Celsius should I convert? Send data like {\"celsius\": 21}.",
    "runtime": {"kind": "inline", "module": "units_demo_ops", "internal": "demo.c_to_f"},
    "timeouts": {"default": 5, "max": 5},
    "input_schema": {"type": "object", "required": ["celsius"],
                     "properties": {"celsius": {"type": "number", "x-aliases": ["temperature", "degrees", "c"]}}},
    "example": {"request": "Convert this temperature from Celsius to Fahrenheit", "data": {"temperature": 100},
                "inputs": {"celsius": 100}, "output": {"fahrenheit": 212.0}},
    "built_on": {"project": "Demo", "license": "MIT", "copyright": "Copyright (c) demo", "source": "https://example.com",
                 "license_file": "licenses/demo.txt", "dependency": {"manager": "pip", "name": "demo", "version": "1.0"}},
}


def test_new_capability_becomes_routable_without_router_changes(tmp_path, monkeypatch):
    request = {"id": "1", "request": "Convert this temperature from Celsius to Fahrenheit", "data": {"temperature": 100}}
    (before,) = run(request)
    assert before["status"] == "needs_clarification"           # nothing registered can do this yet
    router_source = Path(router.__file__).read_bytes()

    (tmp_path / "units_demo_ops.py").write_text(ADAPTER)        # the adapter ...
    (tmp_path / "units.celsius_to_fahrenheit.json").write_text(json.dumps(MANIFEST))  # ... and its manifest
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        registry.load(tmp_path)                                 # what the app does at startup
        (after,) = run(request)
        assert after["status"] == "ok" and after["operation"] == "units.celsius_to_fahrenheit"
        assert after["output"] == {"fahrenheit": 212.0}
        listed = client.get("/v1/operations").json()["operations"]
        assert "units.celsius_to_fahrenheit" in {o["operation"] for o in listed}
        # the existing routes are unaffected by the newcomer
        (fuzz,) = run({"id": "2", "request": "Compare these two names", "data": ["Drift Protein", "Drift Proteins"]})
        assert fuzz["operation"] == "text.similarity"
    finally:
        registry.unregister("units.celsius_to_fahrenheit")
        sys.modules.pop("units_demo_ops", None)
    assert Path(router.__file__).read_bytes() == router_source  # router untouched
    assert "units.celsius_to_fahrenheit" not in catalog.BY_NAME
