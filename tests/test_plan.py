"""Static detection of how a repository is built and run (no database, nothing executed)."""
import json
from pathlib import Path

import pytest

from app.imports import plan, services

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "import_repos"


@pytest.mark.parametrize("repo, runtime", [
    ("geodist", "python"), ("genversion", "python"), ("pycli", "python"), ("shapes", "python"),
    ("nodeslug", "node"), ("nodecli", "node"), ("cssfw", "frontend"), ("cnative", "native"),
    ("pyservice", "service"), ("pyservice2", "service"), ("nodeservice", "service"),
    ("dockeronly", "container"), ("pixiapp", "conda"),
])
def test_runtime_is_detected_from_the_repos_own_files(repo, runtime):
    try:
        p = plan.detect(FIXTURES / repo, repo)
    except plan.NeedsRuntime as exc:
        pytest.fail(f"{repo}: {exc.reason}")
    assert p["runtime"] == runtime


def test_configure_ac_projects_are_native_autotools_builds(tmp_path):
    (tmp_path / "configure.ac").write_text("AC_INIT([tool], [1.0])\n")
    (tmp_path / "Dockerfile").write_text("FROM debian\n")    # a Dockerfile for CI doesn't make it a container
    p = plan.detect(tmp_path, "tool")
    assert p["runtime"] == "native" and p["native"] == {"system": "autotools", "autoreconf": True}


def test_node_server_with_a_framework_dependency_is_a_service(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"name": "api", "main": "index.js",
                                                       "scripts": {"start": "node index.js"},
                                                       "dependencies": {"express": "^4"}}))
    (tmp_path / "index.js").write_text("const app = require('express')(); app.post('/x', (q, r) => r.json({}));")
    assert plan.detect(tmp_path, "api")["runtime"] == "service"


def test_a_library_that_ships_example_apps_is_still_a_library(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "lib"\n')
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "__init__.py").write_text("def f(x: int) -> int:\n    return x\n")
    (tmp_path / "examples").mkdir()
    (tmp_path / "examples" / "server.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
    assert plan.detect(tmp_path, "lib")["runtime"] == "python"


def test_python_web_apps_are_found_statically():
    apps = services.detect_python(FIXTURES / "pyservice")
    assert apps[0] == {"kind": "asgi", "framework": "FastAPI", "module": "main", "app": "app", "file": "main.py",
                       "src_root": ""}


def test_source_routes_and_openapi_endpoints():
    routes = services.routes_from_source(FIXTURES / "nodeservice", "node")
    assert {(r["method"], r["path"]) for r in routes} == {("GET", "/stats"), ("GET", "/healthz")}
    doc = {"paths": {"/items/{item_id}": {"post": {
        "operationId": "update_item", "parameters": [{"name": "item_id", "in": "path", "required": True,
                                                      "schema": {"type": "integer"}}],
        "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/Item"}}}}}}},
        "components": {"schemas": {"Item": {"type": "object", "required": ["name"],
                                            "properties": {"name": {"type": "string", "example": "pen"}}}}}}
    eps = services.endpoints_from_openapi(doc)
    schema, sample = services.input_schema(eps[0])
    assert schema["required"] == ["item_id", "body"] and sample == {"item_id": 1, "body": {"name": "pen"}}
    assert services.request_for({"method": "POST", "path": "/items/{item_id}"}, sample) == ("POST", "/items/1", {},
                                                                                         {"name": "pen"})


def test_versioned_shared_libraries_are_not_programs():
    import re
    assert re.search(r"\.so(\.\d+)+$", "libmd4c.so.0.6.0") and not re.search(r"\.so(\.\d+)+$", "md2html")


def test_configuration_format_accepts_endpoint_choices_and_profiles():
    plan.validate_config({"entry_point": "POST /predict", "profile": "large",
                          "service": {"start": ["python", "-m", "uvicorn", "app:app", "--port", "{port}"]}})
    with pytest.raises(ValueError):
        plan.validate_config({"entry_point": "rm -rf /"})
