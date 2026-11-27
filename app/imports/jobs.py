"""Import jobs: submission (with dedupe), persistent status, and the background worker pipeline.

Pipeline (one job at a time per API instance, claimed with FOR UPDATE SKIP LOCKED):
  fetching -> reading_license (recorded for attribution; a license never blocks) -> analyzing ->
  installing -> discovering (sandbox: find the algorithm, infer schema, real test call) -> registering -> verifying (a real POST /v1/execute
  with a short-lived key of the customer) -> ready.  Any problem -> failed with a specific code
  and reason. Only `ready` imports are callable, and only by the customer who submitted them.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import jsonschema

from app import accounts, catalog, db, manifests
from app.imports import github, installer, licensing, packaging, sandbox
from app.imports.github import ImportFailure

log = logging.getLogger("uvicorn.error")

ACTIVE_LIMIT = 3          # queued + running imports per customer
DAILY_LIMIT = 20          # submissions per customer per 24 h
STALE_AFTER_S = 30 * 60   # a running job not finished by then is retried (worker died)
MAX_ATTEMPTS = 3
DISCOVER_TIMEOUT = 180
CALL_DEFAULT_TIMEOUT, CALL_MAX_TIMEOUT = 30, 120
FAILURE_CODES = {"invalid_url", "repo_not_found", "ref_not_found", "repo_empty", "repo_too_large", "github_unavailable",
                 "unsupported_language", "unsupported_build",
                 "no_python_package", "multiple_packages", "unsupported_dependency", "import_failed",
                 "no_callable_function", "multiple_entry_points", "entry_point_not_found", "unsupported_input",
                 "unsupported_output", "test_failed", "verification_failed", "sandbox_unavailable",
                 "sandbox_crashed", "timeout", "internal_error"}
CONVERT_RE = re.compile(r"^(tuple|set|ndarray|(enum|enumname):[A-Za-z_][\w.]*:[A-Za-z_][\w.]*)$")
IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")


def imports_dir() -> Path:
    explicit = os.environ.get("IMPORTS_DIR")
    if explicit:
        return Path(explicit)
    data = Path("/data")
    if data.is_dir() and os.access(data, os.W_OK):
        return data / "imports"
    return Path(tempfile.gettempdir()) / "cognex-imports"


def _slug(s: str, limit: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")[:limit].strip("_")
    return s if s and s[0].isalpha() else ("r" + s)[:limit]


# -- public view of an import -----------------------------------------------------------------------
COLUMNS = ("id, customer_id, repo_url, repo_full_name, commit_sha, entry_point, status, stage, failure_code, "
           "failure_reason, failure_details, operation, manifest, spec, pins, license_spdx, example_response, "
           "created_at, updated_at, ready_at, attempts, published_name, published_summary, published_at, "
           "published_when_to_use")


def _row(r) -> Dict[str, Any]:
    keys = [c.strip() for c in COLUMNS.split(",")]
    return dict(zip(keys, r))


def public_view(row: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "id": str(row["id"]), "repo": row["repo_full_name"], "url": row["repo_url"], "commit": row["commit_sha"],
        "status": row["status"], "stage": row["stage"], "entry_point": row["entry_point"],
        "created_at": row["created_at"], "updated_at": row["updated_at"], "ready_at": row["ready_at"]}
    if row["status"] == "failed":
        out["failure"] = {"code": row["failure_code"], "reason": row["failure_reason"],
                          **({"details": row["failure_details"]} if row["failure_details"] else {})}
    m = row.get("manifest")
    if row["status"] == "ready" and m:
        body = {"tasks": [{"id": "1", "operation": row["operation"], "inputs": m["example"]["inputs"]}]}
        out.update({
            "operation": row["operation"], "summary": m["summary"],
            "function": (row.get("spec") or {}).get("signature"),
            "input_schema": m["input_schema"],
            "license": {"spdx": row["license_spdx"], "copyright": m["built_on"]["copyright"]},
            "example_request": {
                "method": "POST", "url": f"{base_url}/v1/execute",
                "headers": {"Content-Type": "application/json", "X-API-Key": "<your Cognex API key>"},
                "body": body,
                "curl": (f"curl -sS {base_url}/v1/execute \\\n  -H \"Content-Type: application/json\" \\\n"
                         f"  -H \"X-API-Key: $COGNEX_API_KEY\" \\\n  -d '{json.dumps(body)}'")},
            "example_response": _as_example_response(row["example_response"]),
            "plain_english_example": {"tasks": [{"id": "1", "request": m["example"]["request"],
                                                 "data": m["example"]["data"]}]},
            "credits_per_call": catalog.CREDITS_PER_OPERATION})
    if row.get("published_name"):
        out["published"] = {"operation": row["published_name"], "live": bool(row["published_at"] and
                                                                                 row["status"] == "ready"),
                            "published_at": row["published_at"], "summary": row["published_summary"],
                            "when_to_use": row["published_when_to_use"]}
    return out


def _as_example_response(stored: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The verification call's response, with its task id matching example_request ("1")."""
    if not stored:
        return stored
    return {**stored, "results": [{**r, "id": "1"} for r in stored.get("results", [])]}


# -- submission -------------------------------------------------------------------------------------
class SubmitError(Exception):
    def __init__(self, status: int, code: str, reason: str):
        super().__init__(reason)
        self.status, self.code, self.reason = status, code, reason


def submit(customer_id: str, url: str, entry_point: Optional[str] = None,
           retry: bool = False) -> Tuple[Dict[str, Any], bool]:
    """Validate, pin the commit and queue an import. Returns (row, created). Dedupes per commit."""
    if entry_point is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,99}", entry_point):
        raise SubmitError(400, "invalid_entry_point", "entry_point must be a Python function name.")
    try:
        ref = github.parse_url(url)
        commit = github.resolve_commit(ref)
    except ImportFailure as exc:
        status = {"invalid_url": 400, "github_unavailable": 503}.get(exc.code, 422)
        raise SubmitError(status, exc.code, exc.reason) from None
    with db.pool().connection() as conn:
        conn.execute("INSERT INTO customers (id) VALUES (%s) ON CONFLICT (id) DO NOTHING", (customer_id,))
        conn.execute("INSERT INTO credit_accounts (customer_id) VALUES (%s) ON CONFLICT (customer_id) DO NOTHING",
                     (customer_id,))
        conn.execute("SELECT 1 FROM customers WHERE id = %s FOR UPDATE", (customer_id,))
        existing = conn.execute(f"SELECT {COLUMNS} FROM github_imports WHERE customer_id = %s "
                                f"AND lower(repo_full_name) = lower(%s) AND commit_sha = %s",
                                (customer_id, ref.full_name, commit)).fetchone()
        if existing:
            row = _row(existing)
            if row["status"] == "failed" and (retry or (entry_point and entry_point != row["entry_point"])):
                fresh = conn.execute(
                    f"UPDATE github_imports SET status = 'queued', stage = 'queued', failure_code = NULL, "
                    f"failure_reason = NULL, failure_details = NULL, operation = NULL, manifest = NULL, spec = NULL, "
                    f"entry_point = COALESCE(%s, entry_point), attempts = 0, claimed_by = NULL, updated_at = now() "
                    f"WHERE id = %s RETURNING {COLUMNS}", (entry_point, row["id"])).fetchone()
                return _row(fresh), True
            return row, False
        active, today = conn.execute(
            "SELECT count(*) FILTER (WHERE status IN ('queued', 'running')), "
            "count(*) FILTER (WHERE created_at > now() - interval '1 day') FROM github_imports WHERE customer_id = %s",
            (customer_id,)).fetchone()
        if active >= ACTIVE_LIMIT:
            raise SubmitError(429, "too_many_active_imports",
                              f"You already have {active} imports in progress; wait for one to finish.")
        if today >= DAILY_LIMIT:
            raise SubmitError(429, "daily_import_limit", f"At most {DAILY_LIMIT} imports per day.")
        row = conn.execute(
            f"INSERT INTO github_imports (id, customer_id, repo_url, repo_full_name, commit_sha, entry_point) "
            f"VALUES (%s, %s, %s, %s, %s, %s) RETURNING {COLUMNS}",
            (uuid.uuid4(), customer_id, ref.url + (f"/tree/{ref.ref}" if ref.ref else ""), ref.full_name, commit,
             entry_point)).fetchone()
    return _row(row), True


def list_for(customer_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    with db.pool().connection() as conn:
        rows = conn.execute(f"SELECT {COLUMNS} FROM github_imports WHERE customer_id = %s "
                            f"ORDER BY created_at DESC LIMIT %s", (customer_id, limit)).fetchall()
    return [_row(r) for r in rows]


def get_for(customer_id: str, import_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    with db.pool().connection() as conn:
        r = conn.execute(f"SELECT {COLUMNS} FROM github_imports WHERE id = %s AND customer_id = %s",
                         (import_id, customer_id)).fetchone()
    return _row(r) if r else None


# -- publishing (operator only) -----------------------------------------------------------------------
OPERATOR_OWNER = "cognex-operator"   # account that owns imports the operator adds from GitHub links
PUBLIC_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}\.[a-z][a-z0-9_]{1,59}$")


def get_any(import_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    with db.pool().connection() as conn:
        r = conn.execute(f"SELECT {COLUMNS} FROM github_imports WHERE id = %s", (import_id,)).fetchone()
    return _row(r) if r else None


def list_all(limit: int = 200) -> List[Dict[str, Any]]:
    """Operator view: Cognex's own imports and everything published."""
    with db.pool().connection() as conn:
        rows = conn.execute(f"SELECT {COLUMNS} FROM github_imports WHERE customer_id = %s OR published_name IS NOT "
                            f"NULL ORDER BY created_at DESC LIMIT %s", (OPERATOR_OWNER, limit)).fetchall()
    return [_row(r) for r in rows]


def publish(import_id: uuid.UUID, name: str, summary: Optional[str] = None,
            when_to_use: Optional[str] = None) -> Dict[str, Any]:
    """Publish an operator import under a Cognex name, for every customer. Live as soon as it is
    ready (immediately if it already is). Customer imports stay private and can't be published."""
    if not PUBLIC_NAME_RE.match(name):
        raise SubmitError(400, "invalid_name", "name must look like 'category.action' (lowercase letters, digits, _).")
    if summary is not None and not 10 <= len(summary.strip()) <= 300:
        raise SubmitError(400, "invalid_summary", "summary must be 10-300 characters.")
    if when_to_use is not None and not 10 <= len(when_to_use.strip()) <= 500:
        raise SubmitError(400, "invalid_when_to_use", "when_to_use must be 10-500 characters.")
    if name in catalog.BY_NAME or name in catalog.ALIASES:
        raise SubmitError(409, "name_taken", f"'{name}' is already a built-in Cognex operation.")
    with db.pool().connection() as conn:
        row = conn.execute(f"SELECT {COLUMNS} FROM github_imports WHERE id = %s FOR UPDATE", (import_id,)).fetchone()
        if row is None:
            raise SubmitError(404, "not_found", "import not found")
        row = _row(row)
        if row["customer_id"] != OPERATOR_OWNER:
            raise SubmitError(409, "not_publishable", "Only imports added by Cognex (POST /v1/admin/imports) can be "
                                                      "published; customer imports stay private.")
        if row["status"] == "failed":
            raise SubmitError(409, "import_failed", f"The import failed ({row['failure_code']}); retry it first.")
        clash = conn.execute(
            "SELECT id FROM github_imports WHERE id <> %s AND (lower(published_name) = lower(%s) OR "
            "(operation = %s AND status IN ('running', 'ready')))", (import_id, name, name)).fetchone()
        if clash:
            raise SubmitError(409, "name_taken", f"'{name}' is already used by another operation.")
        live = row["status"] == "ready"
        fresh = conn.execute(
            f"UPDATE github_imports SET published_name = %s, published_summary = %s, published_when_to_use = %s, "
            f"published_at = CASE WHEN published_at IS NOT NULL AND published_name = %s THEN published_at "
            f"WHEN %s THEN now() END, updated_at = now() WHERE id = %s RETURNING {COLUMNS}",
            (name, summary.strip() if summary else None, when_to_use.strip() if when_to_use else None, name, live,
             import_id)).fetchone()
    from app.imports import private
    private.invalidate()
    return _row(fresh)


def unpublish(import_id: uuid.UUID) -> Optional[Dict[str, Any]]:
    with db.pool().connection() as conn:
        r = conn.execute(f"UPDATE github_imports SET published_name = NULL, published_summary = NULL, "
                         f"published_when_to_use = NULL, published_at = NULL, updated_at = now() WHERE id = %s AND customer_id = %s "
                         f"RETURNING {COLUMNS}", (import_id, OPERATOR_OWNER)).fetchone()
    from app.imports import private
    private.invalidate()
    return _row(r) if r else None


# -- worker -------------------------------------------------------------------------------------------
def _update(import_id, **fields) -> None:
    sets = ", ".join(f"{k} = %s" for k in fields) + ", updated_at = now()"
    vals = [json.dumps(v) if k in ("failure_details", "manifest", "spec", "pins", "example_response") and v is not None
            else v for k, v in fields.items()]
    with db.pool().connection() as conn:
        conn.execute(f"UPDATE github_imports SET {sets} WHERE id = %s", (*vals, import_id))


def _fail(import_id, code: str, reason: str, details: Optional[dict] = None) -> None:
    code = code if code in FAILURE_CODES else "internal_error"
    _update(import_id, status="failed", stage="failed", failure_code=code, failure_reason=(reason or "")[:1000],
            failure_details=details, operation=None)
    from app.imports import private
    private.invalidate()


def claim(instance: uuid.UUID) -> Optional[Dict[str, Any]]:
    with db.pool().connection() as conn:
        r = conn.execute(
            f"UPDATE github_imports SET status = 'running', claimed_by = %s, claimed_at = now(), "
            f"attempts = attempts + 1, updated_at = now() WHERE id = ("
            f"  SELECT id FROM github_imports WHERE status = 'queued' OR (status = 'running' AND claimed_at < "
            f"  now() - make_interval(secs => %s) AND attempts < %s) ORDER BY created_at "
            f"  FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING {COLUMNS}", (instance, STALE_AFTER_S, MAX_ATTEMPTS)).fetchone()
        if r is None:  # jobs that kept dying are failed rather than retried forever
            conn.execute("UPDATE github_imports SET status = 'failed', stage = 'failed', failure_code = "
                         "'internal_error', failure_reason = 'The import stopped unexpectedly several times.', "
                         "updated_at = now() WHERE status = 'running' AND attempts >= %s AND claimed_at < "
                         "now() - make_interval(secs => %s)", (MAX_ATTEMPTS, STALE_AFTER_S))
    return _row(r) if r else None


def _validate_discovery(res: Dict[str, Any], module: str) -> None:
    """The discovery result came from the sandbox: check its shape before trusting any of it."""
    if not isinstance(res.get("function"), str) or not IDENT_RE.match(res["function"]) or res.get("module") != module:
        raise ImportFailure("internal_error", "The analysis returned an invalid function reference.")
    params = res.get("params")
    if not isinstance(params, list) or len(params) > 50:
        raise ImportFailure("internal_error", "The analysis returned invalid parameters.")
    for p in params:
        if not isinstance(p, dict) or not IDENT_RE.match(str(p.get("name", ""))) or not isinstance(p.get("schema"), dict):
            raise ImportFailure("internal_error", "The analysis returned an invalid parameter.")
        conv = p.get("convert")
        if conv is not None and (not isinstance(conv, str) or not CONVERT_RE.match(conv) or
                                 (conv.startswith("enum") and not conv.split(":", 1)[1].startswith(module))):
            raise ImportFailure("internal_error", "The analysis returned an invalid input converter.")
    schema = res.get("input_schema")
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(res.get("sample_inputs"), schema)
    except (jsonschema.SchemaError, jsonschema.ValidationError) as exc:
        raise ImportFailure("internal_error", f"The inferred schema is invalid: {exc.message}") from None
    for key, limit in (("input_schema", 64_000), ("sample_inputs", 256_000), ("sample_output", 256_000)):
        if len(json.dumps(res.get(key))) > limit:
            raise ImportFailure("unsupported_output", f"The {key.replace('_', ' ')} is too large to publish.")


def _operation_name(customer_id: str, import_id, repo: str, function: str, commit: str) -> str:
    base = f"{_slug(repo, 30)}.{_slug(function, 40)}"
    taken = set(catalog.BY_NAME) | set(catalog.ALIASES)
    with db.pool().connection() as conn:
        taken |= {r[0] for r in conn.execute(
            "SELECT operation FROM github_imports WHERE customer_id = %s AND id <> %s AND operation IS NOT NULL "
            "AND status IN ('running', 'ready')", (customer_id, import_id)).fetchall()}
        taken |= {r[0] for r in conn.execute(  # published operations are visible to every customer
            "SELECT published_name FROM github_imports WHERE published_name IS NOT NULL").fetchall()}
    name = base if base not in taken else f"{base}_{commit[:7]}"
    return name if name not in taken else f"{base}_{uuid.uuid4().hex[:6]}"


def _manifest(name: str, row: Dict[str, Any], info: packaging.PackageInfo, res: Dict[str, Any],
              spdx: str, copyright_lines: List[str], has_license_file: bool = True) -> Dict[str, Any]:
    fn, repo = res["function"], row["repo_full_name"]
    summary = (res.get("summary") or info.description or f"Run {fn} from {repo}").strip()
    summary = (summary if len(summary) >= 10 else f"{summary} ({fn} from {repo})")[:300]
    words = fn.replace("_", " ")
    intents = list(dict.fromkeys(x for x in [summary.rstrip("."), (info.description or "").rstrip("."), words,
                                             f"run {words}", f"{repo.split('/')[1]} {words}"] if x and len(x) >= 3))
    sample = res["sample_inputs"]
    return {
        "name": name, "summary": summary, "intents": intents[:12],
        "clarify": f"What inputs should I pass to {fn}? Send data like {json.dumps(sample)[:200]}."[:300],
        "runtime": {"kind": "sandbox", "internal": f"{res['module']}:{fn}", "import_id": str(row["id"])},
        "timeouts": {"default": CALL_DEFAULT_TIMEOUT, "max": CALL_MAX_TIMEOUT},
        "input_schema": res["input_schema"],
        "example": {"request": summary.rstrip("."), "data": sample, "inputs": sample, "output": res["sample_output"]},
        "built_on": {"project": repo, "license": spdx,
                     "copyright": "; ".join(copyright_lines)[:500] or f"Copyright the {repo} authors",
                     "source": f"https://github.com/{repo}/tree/{row['commit_sha']}",
                     "license_file": f"imports/{row['id']}/LICENSE" if has_license_file else "none",
                     "dependency": {"manager": "github", "name": repo, "version": row["commit_sha"][:12],
                                    "commit": row["commit_sha"]}},
    }


def _http_verify(customer_id: str, import_id: str, operation: str, inputs: dict) -> Tuple[int, dict]:
    """A real call through POST /v1/execute with a short-lived, scoped, never-billed key of the customer."""
    key = accounts.create_key(customer_id, "Import check (automatic)", kind="import_check", scope=import_id)
    base = os.environ.get("COGNEX_SELF_URL") or f"http://127.0.0.1:{os.environ.get('PORT', '8000')}"
    try:
        req = urllib.request.Request(f"{base}/v1/execute", method="POST", data=json.dumps(
            {"tasks": [{"id": "import-check", "operation": operation, "inputs": inputs}]}).encode(),
            headers={"Content-Type": "application/json", "X-API-Key": key["secret"]})
        try:
            with urllib.request.urlopen(req, timeout=CALL_MAX_TIMEOUT + 60) as r:  # noqa: S310 (own server)
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as exc:
            return exc.code, {"error": exc.read().decode("utf-8", "replace")[:500]}
    finally:
        accounts.revoke_key(customer_id, uuid.UUID(key["id"]))


VERIFY: Callable[[str, str, str, dict], Tuple[int, dict]] = _http_verify  # tests swap in an in-process client


def process(row: Dict[str, Any]) -> None:
    import_id = row["id"]
    work = imports_dir() / str(import_id)
    src, site = work / "src", work / "site"
    try:
        ref = github.parse_url(row["repo_url"])
        _update(import_id, stage="fetching")
        FETCH(ref, row["commit_sha"], src)
        _update(import_id, stage="reading_license")  # recorded for attribution; never accepts or rejects
        spdx, lic = licensing.attribution(licensing.license_files(src))
        if lic is not None:
            shutil.copyfile(src / lic["file"], work / "LICENSE")
        _update(import_id, stage="analyzing", license_spdx=spdx, license_text=lic["text"][:200_000] if lic else None)
        info = packaging.analyze(src, ref.repo)
        _update(import_id, stage="installing")
        pins = installer.install(info.dependencies, site)
        _update(import_id, stage="discovering", pins=pins)
        res = sandbox.run("discover", {"module": info.module, "repo_name": ref.repo, "readme": info.readme,
                                       "entry_point": row["entry_point"]},
                          paths=[str(info.root), str(site)], timeout=DISCOVER_TIMEOUT, scratch_root=work.parent / "tmp")
        if not res.get("ok"):
            raise ImportFailure(str(res.get("code") or "internal_error"), str(res.get("reason") or res.get("error")),
                                **({"candidates": res["candidates"][:10]} if isinstance(res.get("candidates"), list) else {}))
        _validate_discovery(res, info.module)
        _update(import_id, stage="registering")
        name = _operation_name(row["customer_id"], import_id, ref.repo, res["function"], row["commit_sha"])
        manifest = _manifest(name, row, info, res, spdx, lic["copyright_lines"] if lic else [],
                             has_license_file=lic is not None)
        manifests.parse(manifest)  # same format checks as built-in operations
        spec = {"module": info.module, "function": res["function"], "params": res["params"],
                "root": str(info.root.relative_to(src)).replace("\\", "/") if info.root != src else "",
                "signature": res.get("signature")}
        (work / "ready.json").write_text(json.dumps({"commit": row["commit_sha"], "pins": pins}))
        _update(import_id, stage="verifying", operation=name, manifest=manifest, spec=spec)
        status, body = VERIFY(row["customer_id"], str(import_id), name, res["sample_inputs"])
        result = (body.get("results") or [{}])[0] if isinstance(body, dict) else {}
        if status != 200 or result.get("status") != "ok" or result.get("operation") != name:
            why = result.get("error") or body.get("error") or f"HTTP {status}"
            raise ImportFailure("verification_failed",
                                f"The test call through POST /v1/execute didn't succeed: {str(why)[:400]}")
        _update(import_id, status="ready", stage="ready", example_response={"results": [result]})
        with db.pool().connection() as conn:  # a name reserved at submit goes live now
            conn.execute("UPDATE github_imports SET ready_at = now(), published_at = CASE WHEN published_name "
                         "IS NOT NULL THEN now() END WHERE id = %s", (import_id,))
        from app.imports import private
        private.invalidate()
        log.info("import %s ready as %s", import_id, name)
    except ImportFailure as exc:
        log.info("import %s failed: %s", import_id, exc.code)
        _fail(import_id, exc.code, exc.reason, exc.details or None)
        shutil.rmtree(work, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001 - never leave a job running
        log.exception("import %s crashed", import_id)
        _fail(import_id, "internal_error", f"The import failed unexpectedly ({type(exc).__name__}).")
        shutil.rmtree(work, ignore_errors=True)


FETCH: Callable = github.download  # tests swap in a local fixture copier


class ImportWorker(threading.Thread):
    """Background thread: claims queued imports and processes them one at a time."""

    def __init__(self, poll_s: float = 2.0):
        super().__init__(name="github-imports", daemon=True)
        self.stop = threading.Event()
        self.poll_s = poll_s
        self.instance = uuid.uuid4()

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                row = claim(self.instance)
            except Exception as exc:  # noqa: BLE001 - database hiccup: try again later
                log.error("import queue unavailable: %s", type(exc).__name__)
                row = None
            if row is None:
                self.stop.wait(self.poll_s)
                continue
            process(row)


def enabled() -> bool:
    return os.environ.get("COGNEX_IMPORTS", "on").strip().lower() != "off"
