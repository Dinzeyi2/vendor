"""Imported operations: a customer's own (private) and Cognex's published ones, run in the sandbox.

Private by construction: capabilities are looked up by the caller's customer_id only, never added
to the global registry or catalog, so another account can neither list nor call them (an
unknown name is simply unknown_operation). Only `ready` imports are callable; the one exception
is Cognex's own scoped verification key, which may call its import while it is being verified.

Published operations are imports the operator added and published under a Cognex name
(`published()`): every API key can list and call them, billed like any other operation.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from functools import partial
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from app import db, manifests
from app.errors import OperationError
from app.imports import github, installer, sandbox
from app.manifests import Capability
from app.workers import WorkerFailure

TTL_S = 10.0
_cache: Dict[Tuple[str, Optional[str]], Tuple[float, Dict[str, Tuple[Capability, Any]]]] = {}
_cache_lock = threading.Lock()
_build_locks: Dict[str, threading.Lock] = {}


def invalidate() -> None:
    global _published
    with _cache_lock:
        _cache.clear()
        _published = (0.0, {})


def capabilities_for(customer_id: str, scope: Optional[str] = None) -> Dict[str, Tuple[Capability, Any]]:
    """{operation: (capability, OpSpec)} for this customer's ready imports (or, for an import-check
    key, only its scoped import, which may still be verifying)."""
    key = (customer_id, scope)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
    from app.registry import OpSpec
    if scope:
        try:
            scope_id = uuid.UUID(scope)
        except ValueError:
            return {}
        where, params = ("id = %s AND customer_id = %s AND (status = 'ready' OR (status = 'running' "
                         "AND stage = 'verifying'))", (scope_id, customer_id))
    else:
        where, params = "customer_id = %s AND status = 'ready'", (customer_id,)
    with db.pool().connection() as conn:
        rows = conn.execute(f"SELECT id, operation, manifest, spec, pins, repo_url, repo_full_name, commit_sha "
                            f"FROM github_imports WHERE {where} AND operation IS NOT NULL", params).fetchall()
    out: Dict[str, Tuple[Capability, Any]] = {}
    for iid, op, manifest, spec, pins, url, full_name, commit in rows:
        try:
            cap = manifests.parse(manifest)
        except manifests.ManifestError:
            continue
        runner = partial(execute, str(iid), spec, pins or [], url, commit)
        out[op] = (cap, OpSpec(full_name, cap.default_timeout, cap.max_timeout, internal=cap.internal, runner=runner))
    with _cache_lock:
        _cache[key] = (now + TTL_S, out)
    return out


_published: Tuple[float, Dict[str, Tuple[Capability, Any]]] = (0.0, {})


def published() -> Dict[str, Tuple[Capability, Any]]:
    """{public name: (capability, OpSpec)} for every live published import (all callers)."""
    global _published
    now = time.monotonic()
    with _cache_lock:
        if _published[0] > now:
            return _published[1]
    from app.registry import OpSpec
    with db.pool().connection() as conn:
        rows = conn.execute("SELECT id, published_name, published_summary, manifest, spec, pins, repo_url, "
                            "repo_full_name, commit_sha FROM github_imports WHERE published_at IS NOT NULL "
                            "AND status = 'ready' AND manifest IS NOT NULL").fetchall()
    out: Dict[str, Tuple[Capability, Any]] = {}
    for iid, name, summary, manifest, spec, pins, url, full_name, commit in rows:
        m = {**manifest, "name": name, "summary": summary or manifest["summary"]}
        if summary:
            m["intents"] = list(dict.fromkeys([summary.rstrip("."), *manifest.get("intents", [])]))[:12]
            m["example"] = {**manifest["example"], "request": summary.rstrip(".")}
        try:
            cap = manifests.parse(m)
        except manifests.ManifestError:
            continue
        runner = partial(execute, str(iid), spec, pins or [], url, commit)
        out[name] = (cap, OpSpec(full_name, cap.default_timeout, cap.max_timeout, internal=cap.internal, runner=runner))
    with _cache_lock:
        _published = (now + TTL_S, out)
    return out


def _ensure_artifacts(import_id: str, pins, url: str, commit: str) -> Path:
    from app.imports.jobs import FETCH, imports_dir
    work = imports_dir() / import_id
    if (work / "ready.json").is_file() and (work / "src").is_dir():
        return work
    lock = _build_locks.setdefault(import_id, threading.Lock())
    with lock:  # rebuild after a redeploy wiped the disk: same commit, same pinned wheels
        if not (work / "ready.json").is_file():
            FETCH(github.parse_url(url), commit, work / "src")
            installer.reinstall(list(pins), work / "site")
            (work / "ready.json").write_text(json.dumps({"commit": commit, "pins": list(pins)}))
    return work


def execute(import_id: str, spec: Dict[str, Any], pins, url: str, commit: str, inputs: Dict[str, Any],
            timeout: float, on_start=None) -> Dict[str, Any]:
    try:
        work = _ensure_artifacts(import_id, pins, url, commit)
    except github.ImportFailure as exc:
        raise WorkerFailure("worker_crashed", f"the imported code couldn't be prepared: {exc.reason}",
                            started=False) from None
    root = work / "src" / spec.get("root", "") if spec.get("root") else work / "src"
    res = sandbox.run("call", {"spec": spec, "inputs": inputs}, paths=[str(root), str(work / "site")],
                      timeout=timeout, scratch_root=work.parent / "tmp", on_start=on_start)
    if res.get("ok") and isinstance(res.get("output"), dict):
        return res["output"]
    error_type, message = res.get("error_type") or "operation_failed", str(res.get("error") or "failed")[:500]
    if error_type == "invalid_input":
        raise OperationError(message)
    if error_type not in ("timeout", "worker_crashed", "operation_failed"):
        error_type = "operation_failed"
    raise WorkerFailure(error_type, message, started=True)
