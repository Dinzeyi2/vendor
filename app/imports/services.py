"""HTTP services: repos whose entry point is a web app (Python ASGI/WSGI or a Node.js server).

Shared by every service repo, no per-repo code:
  detect    find the app statically: a module-level `app = FastAPI(...)` / `Flask(__name__)` / ... in
            Python, or a Node package whose dependencies include a server framework and that has a
            start script or main file
  start     run it in the sandbox in "serve" mode (TCP listen only: no connect(), no UDP), on a
            private port given as $PORT, bound to 127.0.0.1
  endpoints from /openapi.json when the app publishes one (FastAPI, Starlette, Litestar...), else
            from its route declarations (@app.get("/x"), app.post('/y', ...))
  sample    OpenAPI examples, else values generated from the schema, else a parameterless GET
  call      start, send one HTTP request from outside the sandbox, return {status, body}, stop
Several plausible endpoints -> multiple_entry_points with the candidates ("POST /predict"); the
choice is saved as the repo configuration's entry_point.
"""
from __future__ import annotations

import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.config import node_bin, npm_cli
from app.imports import sandbox
from app.imports.github import ImportFailure

STARTUP_TIMEOUT = 120
MAX_BODY = 8 * 1024 * 1024
SKIP = {"tests", "test", "docs", "doc", "docs_src", "examples", "example", "benchmarks", "scripts", "node_modules",
        ".git", "build", "dist", "site-packages", "venv", ".venv", "migrations"}
PY_APPS = {"FastAPI": "asgi", "Starlette": "asgi", "Litestar": "asgi", "Quart": "asgi", "Sanic": "asgi",
           "Flask": "wsgi"}
PY_APP_RE = re.compile(r"^([A-Za-z_]\w*)\s*(?::\s*\w+\s*)?=\s*(?:[\w.]+\.)?(" + "|".join(PY_APPS) + r")\s*\(", re.M)
PY_ROUTE_RE = re.compile(r"@\s*[A-Za-z_]\w*\.(get|post|put|patch|delete|route|api_route)\(\s*[rf]?['\"]([^'\"]+)['\"]"
                         r"(?:[^)]*?methods\s*=\s*\[([^\]]*)\])?", re.S)
JS_ROUTE_RE = re.compile(r"\b(?:app|router|server|api)\.(get|post|put|patch|delete)\(\s*['\"`]([^'\"`]+)['\"`]")
BORING = re.compile(r"^/(?:$|health|healthz|ready|readyz|live|livez|ping|status|metrics|docs|redoc|openapi|favicon|"
                    r"static|assets|swagger|robots\.txt)")


# -- detection (static, trusted: reads files only) ----------------------------------------------------
def detect_python(base: Path) -> List[Dict[str, Any]]:
    """Module-level web-app objects, best candidates first."""
    found = []
    for p in sorted(base.rglob("*.py"))[:3000]:
        rel = p.relative_to(base)
        if set(rel.parts[:-1]) & SKIP or p.stat().st_size > 300_000:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for var, cls in PY_APP_RE.findall(text):
            parts = list(rel.with_suffix("").parts)
            if parts[0] == "src":
                parts = parts[1:]
            if not all(x.isidentifier() for x in parts):
                continue
            module = ".".join(parts)
            prio = (len(parts), 0 if rel.stem in ("main", "app", "server", "api", "asgi", "wsgi") else 1)
            found.append({"kind": PY_APPS[cls], "framework": cls, "module": module, "app": var, "file": str(rel),
                          "src_root": "src" if rel.parts[0] == "src" else "", "_prio": prio})
    found.sort(key=lambda f: f["_prio"])
    for f in found:
        f.pop("_prio")
    return found


def python_start(app: Dict[str, Any]) -> List[str]:
    target = f"{app['module']}:{app['app']}"
    if app["kind"] == "asgi":
        return [sys.executable, "-m", "uvicorn", target, "--host", "127.0.0.1", "--port", "{port}",
                "--no-access-log", "--log-level", "warning"]
    code = ("import importlib, os; from werkzeug.serving import run_simple; "
            f"m = importlib.import_module({app['module']!r}); "
            f"run_simple('127.0.0.1', int(os.environ['PORT']), getattr(m, {app['app']!r}), threaded=True)")
    return [sys.executable, "-c", code]


def node_start(base: Path, node: Dict[str, Any]) -> Optional[List[str]]:
    pkg = json.loads((base / "package.json").read_text(encoding="utf-8", errors="replace"))
    start = (pkg.get("scripts") or {}).get("start") or ""
    m = re.fullmatch(r"\s*node\s+([\w./-]+\.(?:c?js|mjs))\s*", start)
    if m and (base / m.group(1)).is_file():
        return [node_bin(), m.group(1)]
    if start and npm_cli():
        return [node_bin(), npm_cli(), "run", "start", "--silent"]
    main = pkg.get("main") or "index.js"
    return [node_bin(), main] if (base / main).is_file() else None


def routes_from_source(base: Path, runtime: str) -> List[Dict[str, Any]]:
    out, seen = [], set()
    exts = ("*.py",) if runtime == "python" else ("*.js", "*.mjs", "*.cjs", "*.ts")
    for ext in exts:
        for p in sorted(base.rglob(ext))[:2000]:
            if set(p.relative_to(base).parts[:-1]) & SKIP:
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
            for m in (PY_ROUTE_RE if runtime == "python" else JS_ROUTE_RE).finditer(text):
                verb, path = m.group(1).upper(), m.group(2)
                verbs = [verb]
                if verb in ("ROUTE", "API_ROUTE"):
                    verbs = re.findall(r"[A-Z]+", (m.group(3) or "'GET'").upper()) if runtime == "python" else ["GET"]
                path = re.sub(r"<(?:\w+:)?(\w+)>", r"{\1}", path)          # Flask <int:id> -> {id}
                path = re.sub(r":(\w+)", r"{\1}", path)                      # Express :id -> {id}
                for v in verbs:
                    if (v, path) not in seen and path.startswith("/"):
                        seen.add((v, path))
                        out.append({"method": v, "path": path, "from_source": True})
    return out


# -- running ------------------------------------------------------------------------------------------
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Running:
    def __init__(self, svc: sandbox.Service, port: int):
        self.svc, self.port = svc, port

    def request(self, method: str, path: str, query: Optional[dict] = None, body: Any = None,
                timeout: float = 60) -> Dict[str, Any]:
        url = f"http://127.0.0.1:{self.port}{path}" + (("?" + urllib.parse.urlencode(query, doseq=True)) if query else "")
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (our sandboxed service, loopback)
                status, ctype, raw = r.status, r.headers.get("Content-Type", ""), r.read(MAX_BODY + 1)
        except urllib.error.HTTPError as exc:
            status, ctype, raw = exc.code, exc.headers.get("Content-Type", ""), exc.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise ImportFailure("unsupported_output", "the service's response is larger than 8 MB")
        text = raw.decode("utf-8", "replace")
        try:
            parsed: Any = json.loads(text) if "json" in ctype or text[:1] in "[{" else text
        except json.JSONDecodeError:
            parsed = text
        return {"status": status, "content_type": ctype.split(";")[0], "body": parsed}


def start(spec: Dict[str, Any], build: Dict[str, Any], memory_mb: int, cpu_seconds: int) -> Running:
    port = _free_port()
    argv = [str(a).replace("{port}", str(port)) for a in spec["start"]]
    env = {**(spec.get("env") or {}), "PORT": str(port), "HOST": "127.0.0.1", "BIND": f"127.0.0.1:{port}",
           "NODE_ENV": "production"}
    if build["runtime"] == "service" and build.get("service_runtime") == "python":
        env["PYTHONPATH"] = os.pathsep.join([build["app_dir"], *build["paths"]])
    svc = sandbox.serve({"argv": argv, "env": env, "cwd": build["app_dir"]}, paths=[], memory_mb=memory_mb,
                        cpu_seconds=cpu_seconds, scratch_root=Path(build["scratch_root"]))
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if not svc.alive():
            tail = svc.stderr_tail()
            svc.stop()
            raise ImportFailure("service_failed", "The service exited while starting: " + " ".join(tail.split())[-700:],
                                log_tail=tail[-1500:])
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return Running(svc, port)
        except OSError:
            time.sleep(0.25)
    tail = svc.stderr_tail()
    svc.stop()
    raise ImportFailure("service_failed", f"The service didn't start listening within {STARTUP_TIMEOUT}s (it was "
                                          f"given $PORT={port}). Last output: " + " ".join(tail.split())[-500:],
                        log_tail=tail[-1500:])


# -- endpoints and schemas ----------------------------------------------------------------------------
def _resolve(schema: Any, root: dict, depth: int = 0) -> Any:
    if depth > 12:
        return {}
    if isinstance(schema, dict):
        if "$ref" in schema and isinstance(schema["$ref"], str) and schema["$ref"].startswith("#/"):
            target: Any = root
            for part in schema["$ref"][2:].split("/"):
                target = target.get(part, {}) if isinstance(target, dict) else {}
            return _resolve(target, root, depth + 1)
        return {k: _resolve(v, root, depth + 1) for k, v in schema.items()
                if k not in ("discriminator", "xml", "externalDocs")}
    if isinstance(schema, list):
        return [_resolve(x, root, depth + 1) for x in schema]
    return schema


def _example(schema: dict, depth: int = 0) -> Any:
    if not isinstance(schema, dict) or depth > 8:
        return None
    for key in ("example", "default"):
        if key in schema:
            return schema[key]
    if schema.get("examples"):
        ex = schema["examples"]
        return ex[0] if isinstance(ex, list) else next(iter(ex.values()), {}).get("value") if isinstance(ex, dict) else None
    if "enum" in schema:
        return schema["enum"][0]
    for key in ("anyOf", "oneOf", "allOf"):
        if schema.get(key):
            subs = [s for s in schema[key] if s.get("type") != "null"] or schema[key]
            if key == "allOf":
                merged: Dict[str, Any] = {}
                for s in subs:
                    v = _example(s, depth + 1)
                    if isinstance(v, dict):
                        merged.update(v)
                return merged
            return _example(subs[0], depth + 1)
    t = schema.get("type")
    if t == "object" or "properties" in schema:
        req = schema.get("required") or list((schema.get("properties") or {}))[:3]
        return {k: _example(v, depth + 1) for k, v in (schema.get("properties") or {}).items() if k in req}
    if t == "array":
        return [_example(schema.get("items") or {"type": "string"}, depth + 1)]
    return {"string": "example", "integer": 1, "number": 1.5, "boolean": True}.get(t, "example")


def endpoints_from_openapi(doc: dict) -> List[Dict[str, Any]]:
    out = []
    for path, item in (doc.get("paths") or {}).items():
        for method, op in (item or {}).items():
            if method.upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE") or not isinstance(op, dict):
                continue
            params = [_resolve(p, doc) for p in (item.get("parameters") or []) + (op.get("parameters") or [])]
            body_schema, body_example = None, None
            content = ((_resolve(op.get("requestBody") or {}, doc)).get("content") or {})
            media = content.get("application/json") or next(iter(content.values()), None)
            if media:
                body_schema = media.get("schema") or {}
                body_example = media.get("example") or (next(iter((media.get("examples") or {}).values()), {}) or {}).get("value")
            responses = op.get("responses") or {}
            ok = next((responses[c] for c in ("200", "201", "202") if c in responses), {})
            resp_media = ((_resolve(ok, doc).get("content") or {}).get("application/json") or {})
            out.append({"method": method.upper(), "path": path, "operation_id": op.get("operationId"),
                        "summary": op.get("summary") or op.get("description") or "",
                        "path_params": [p for p in params if p.get("in") == "path"],
                        "query_params": [p for p in params if p.get("in") == "query"],
                        "body_schema": body_schema, "body_example": body_example,
                        "response_schema": resp_media.get("schema")})
    return out


def _label(ep: Dict[str, Any]) -> str:
    return f"{ep['method']} {ep['path']}"


def choose(endpoints: List[Dict[str, Any]], entry: Optional[str]) -> Dict[str, Any]:
    if entry:
        hit = next((e for e in endpoints if entry in (_label(e), e.get("operation_id"))), None)
        if hit is None:
            raise ImportFailure("entry_point_not_found", f"'{entry}' isn't one of the service's endpoints: "
                                + ", ".join(_label(e) for e in endpoints[:15]), candidates=[_label(e) for e in endpoints[:15]])
        return hit
    useful = [e for e in endpoints if not BORING.match(e["path"])]
    if len(useful) == 1:
        return useful[0]
    from app.imports.plan import NeedsConfig
    if not useful:
        raise NeedsConfig("The service exposes no endpoint besides health, docs or static files.",
                          [{"field": "entry_point", "why": "the endpoint to expose, as 'METHOD /path'"}],
                          {"entry_point": "POST /path"}, candidates=[_label(e) for e in endpoints[:15]])
    posts = [e for e in useful if e["method"] in ("POST", "PUT") and (e.get("body_schema") is not None)]
    if len(posts) == 1:
        return posts[0]
    raise NeedsConfig("The service has several endpoints that could be the operation: "
                      + ", ".join(_label(e) for e in useful[:10]) + ". Pick one; it is saved as the repo's "
                      "configuration.", [{"field": "entry_point", "why": "the endpoint to expose, as 'METHOD /path'"}],
                      {"entry_point": _label(useful[0])}, code="multiple_entry_points",
                      candidates=[_label(e) for e in useful[:15]])


def input_schema(ep: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """(JSON Schema of the operation's inputs, sample inputs)."""
    props, required, sample = {}, [], {}
    for p in ep.get("path_params") or [{"name": n, "schema": {"type": "string"}} for n in re.findall(r"\{(\w+)\}", ep["path"])]:
        props[p["name"]] = p.get("schema") or {"type": "string"}
        required.append(p["name"])
        sample[p["name"]] = p.get("example", _example(props[p["name"]]))
    query = ep.get("query_params") or []
    if query:
        props["query"] = {"type": "object", "properties": {q["name"]: q.get("schema") or {} for q in query},
                          "required": [q["name"] for q in query if q.get("required")]}
        qs = {q["name"]: q.get("example", _example(q.get("schema") or {})) for q in query if q.get("required")}
        if qs:
            sample["query"] = qs
            required.append("query")
    if ep.get("body_schema") is not None:
        props["body"] = ep["body_schema"] or {}
        required.append("body")
        sample["body"] = ep.get("body_example") if ep.get("body_example") is not None else _example(ep["body_schema"])
    if ep.get("from_source"):  # routes read from source carry no parameter information: accept any
        props["query"] = {"type": "object", "additionalProperties": {"type": "string"}}
        if ep["method"] in ("POST", "PUT", "PATCH"):
            props["body"] = {}
    return {"type": "object", "properties": props, "required": required}, sample


def request_for(spec: Dict[str, Any], inputs: Dict[str, Any]) -> Tuple[str, str, dict, Any]:
    path = spec["path"]
    for name in re.findall(r"\{(\w+)\}", path):
        if name not in inputs:
            raise ImportFailure("invalid_input", f"missing path parameter '{name}'")
        path = path.replace("{" + name + "}", urllib.parse.quote(str(inputs[name]), safe=""))
    return spec["method"], path, inputs.get("query") or {}, inputs.get("body")


# -- discovery and calls ------------------------------------------------------------------------------
def discover(plan: Dict[str, Any], build: Dict[str, Any], entry: Optional[str], memory_mb: int) -> Dict[str, Any]:
    op = plan.get("operation") or {}
    spec = {"kind": "http", "start": build["start"], "env": build.get("env") or {}}
    running = start(spec, build, memory_mb, cpu_seconds=900)
    try:
        endpoints, source = [], "routes"
        try:
            doc = running.request("GET", "/openapi.json", timeout=20)
            if doc["status"] == 200 and isinstance(doc["body"], dict) and doc["body"].get("paths"):
                endpoints, source = endpoints_from_openapi(doc["body"]), "openapi"
        except (ImportFailure, OSError, ValueError):
            pass
        if not endpoints:
            endpoints = routes_from_source(Path(build["app_dir"]), build["service_runtime"])
        if op.get("kind") == "http":
            ep = {"method": op["method"].upper(), "path": op["path"], "summary": op.get("summary", "")}
        else:
            if not endpoints:
                from app.imports.plan import NeedsConfig
                raise NeedsConfig("The service started, but Cognex found no endpoints (no /openapi.json and no route "
                                  "declarations it recognizes).", [{"field": "operation", "why": "the endpoint, as "
                                                                    "method + path, with a sample input"}],
                                  {"operation": {"kind": "http", "method": "POST", "path": "/path",
                                                 "input_schema": {"type": "object", "properties": {"body": {}}},
                                                 "sample_inputs": {"body": {}}}})
            ep = choose(endpoints, entry or plan.get("entry_point"))
        schema, sample = (op["input_schema"], op["sample_inputs"]) if "input_schema" in op else input_schema(ep)
        spec.update({"method": ep["method"], "path": ep["path"]})
        method, path, query, body = request_for(spec, sample)
        res = running.request(method, path, query, body, timeout=120)
        if not 200 <= res["status"] < 300:
            raise ImportFailure("test_failed", f"Smoke test {method} {path} with {json.dumps(sample)[:200]} answered "
                                              f"HTTP {res['status']}: {json.dumps(res['body'])[:300]}",
                                endpoint=_label(ep), sample_inputs=sample)
    finally:
        running.svc.stop()
    name = ep.get("operation_id") or re.sub(r"[^a-z0-9]+", "_", f"{ep['method']} {ep['path']}".lower()).strip("_")
    return {"spec": spec, "function": name, "summary": ep.get("summary") or f"{ep['method']} {ep['path']} of the service",
            "input_schema": schema, "sample_inputs": sample, "sample_output": res,
            "signature": f"{ep['method']} {ep['path']} ({source})", "endpoints": [_label(e) for e in endpoints[:30]]}


def call(spec: Dict[str, Any], build: Dict[str, Any], inputs: Dict[str, Any], timeout: float, on_start=None,
         memory_mb: int = 2048) -> Dict[str, Any]:
    try:
        method, path, query, body = request_for(spec, inputs)
    except ImportFailure as exc:
        return {"ok": False, "error_type": "invalid_input", "error": exc.reason}
    if on_start is not None:
        on_start()
    try:
        running = start(spec, build, memory_mb, cpu_seconds=int(timeout) + STARTUP_TIMEOUT)
    except ImportFailure as exc:
        return {"ok": False, "error_type": "worker_crashed", "error": exc.reason[:500]}
    try:
        res = running.request(method, path, query, body, timeout=timeout)
    except (OSError, ImportFailure) as exc:
        return {"ok": False, "error_type": "timeout" if isinstance(exc, TimeoutError) else "operation_failed",
                "error": str(getattr(exc, "reason", exc))[:500]}
    finally:
        running.svc.stop()
    if res["status"] >= 500:
        return {"ok": False, "error_type": "operation_failed", "error": f"the service answered HTTP {res['status']}: "
                                                                       f"{json.dumps(res['body'])[:300]}"}
    return {"ok": True, "output": res}
