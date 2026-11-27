"""HTTP endpoints for customer GitHub imports (website backend auth, scoped to the signed-in customer).

- POST /v1/account/imports          {"url": "https://github.com/OWNER/REPO", optional "entry_point", "retry"}
                                     202 queued (new) / 200 existing import of the same repo + commit
- GET  /v1/account/imports          this customer's imports, newest first
- GET  /v1/account/imports/{id}     one import: status, stage, failure reason, or (ready) the
                                     operation name, schema and a copyable POST /v1/execute example
- GET  /v1/admin/imports/sandbox-selftest   operator: prove what the sandbox blocks on this server

Operator (X-API-Key = the operator key): add a GitHub repo and publish it for every customer.
- POST /v1/admin/imports                 {"url", optional "entry_point", "retry", "publish": {"name", "summary"}}
                                          the same pipeline; with "publish" it goes live for all customers
                                          as soon as it is Ready
- GET  /v1/admin/imports                 Cognex's imports and everything published
- GET  /v1/admin/imports/{id}
- POST /v1/admin/imports/{id}/publish    {"name": "category.action", optional "summary", "when_to_use"}
- POST /v1/admin/imports/{id}/unpublish
- POST /v1/admin/imports/{id}/refresh-attribution   re-detect license + copyright from the pinned commit

Repo configurations (operator): the one standard way to fix a repo Cognex can't fully detect.
- GET    /v1/admin/repo-configs                       all saved configurations
- GET    /v1/admin/repo-configs/schema                the configuration format (JSON Schema)
- GET    /v1/admin/repo-configs/{owner}/{repo}
- PUT    /v1/admin/repo-configs/{owner}/{repo}        save (validated); re-queues that repo's failed imports
- DELETE /v1/admin/repo-configs/{owner}/{repo}
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app import db, discovery
from app.account_api import signed_in_customer
from app.billing_api import require_operator
from app.imports import configs, jobs, plan as plans, sandbox

router = APIRouter()


_base_url = discovery.base_url


def _call(fn, *args):
    try:
        return fn(*args)
    except db.DatabaseUnavailable as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from None


class ImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=10, max_length=300, description="Public GitHub repository URL")
    entry_point: Optional[str] = Field(default=None, max_length=220,
                                       description="Optional: the function to use when a repo has several")
    retry: bool = Field(default=False, description="Retry a failed import of the same commit")


@router.post("/v1/account/imports", tags=["imports (website backend only)"], status_code=202,
             responses={200: {"description": "This repo + commit was already imported; existing import returned"},
                        202: {"description": "Queued"}, 400: {"description": "invalid_url"},
                        422: {"description": "repo_not_found / ref_not_found"}, 429: {"description": "rate limited"}})
def submit_import(body: ImportRequest, request: Request, customer_id: str = Depends(signed_in_customer)):
    try:
        row, created = _call(jobs.submit, customer_id, body.url, body.entry_point, body.retry)
    except jobs.SubmitError as exc:
        return JSONResponse(status_code=exc.status, content={"error": exc.code, "message": exc.reason})
    return JSONResponse(status_code=202 if created else 200,
                        content=jsonable_encoder(jobs.public_view(row, _base_url(request))))


@router.get("/v1/account/imports", tags=["imports (website backend only)"])
def list_imports(request: Request, customer_id: str = Depends(signed_in_customer)):
    base = _base_url(request)
    return {"imports": [jobs.public_view(r, base) for r in _call(jobs.list_for, customer_id)]}


@router.get("/v1/account/imports/{import_id}", tags=["imports (website backend only)"])
def get_import(import_id: uuid.UUID, request: Request, customer_id: str = Depends(signed_in_customer)):
    row = _call(jobs.get_for, customer_id, import_id)
    if row is None:  # not found, or another customer's import: same answer either way
        raise HTTPException(status.HTTP_404_NOT_FOUND, "import not found")
    return jobs.public_view(row, _base_url(request))


_REPO = r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$"
_NAME = r"^[A-Za-z0-9._-]{1,100}$"


@router.get("/v1/admin/repo-configs", dependencies=[Depends(require_operator)], include_in_schema=False)
def list_repo_configs():
    return {"configs": jsonable_encoder(_call(configs.list_all))}


@router.get("/v1/admin/repo-configs/schema", dependencies=[Depends(require_operator)], include_in_schema=False)
def repo_config_schema():
    return plans.CONFIG_SCHEMA


@router.get("/v1/admin/repo-configs/{owner}/{repo}", dependencies=[Depends(require_operator)], include_in_schema=False)
def get_repo_config(owner: str, repo: str):
    found = _call(configs.view, f"{owner}/{repo}")
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no configuration saved for this repository")
    return jsonable_encoder(found)


@router.put("/v1/admin/repo-configs/{owner}/{repo}", dependencies=[Depends(require_operator)], include_in_schema=False)
def put_repo_config(owner: str, repo: str, body: dict):
    import re as _re
    if not _re.match(_REPO, owner) or not _re.match(_NAME, repo):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "use the repository's OWNER/REPO")
    try:
        saved = _call(configs.save, f"{owner}/{repo}", body)
    except ValueError as exc:
        return JSONResponse(status_code=422, content={"error": "invalid_config", "message": str(exc)})
    requeued = _call(jobs.requeue_failed, f"{owner}/{repo}")
    return jsonable_encoder({**saved, "requeued_imports": requeued})


@router.delete("/v1/admin/repo-configs/{owner}/{repo}", dependencies=[Depends(require_operator)], include_in_schema=False)
def delete_repo_config(owner: str, repo: str):
    if not _call(configs.delete, f"{owner}/{repo}"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no configuration saved for this repository")
    return {"deleted": f"{owner}/{repo}".lower()}


@router.get("/v1/admin/imports/sandbox-selftest", dependencies=[Depends(require_operator)], include_in_schema=False)
def sandbox_selftest():
    """Runs Cognex's own probe inside the sandbox (no customer code) and reports what it could do."""
    from app.config import node_bin
    res = sandbox.run("selftest", {"app_dir": "/app", "alloc_bytes": 3 * 1024 ** 3, "node": node_bin()}, paths=[],
                      timeout=90, scratch_root=jobs.imports_dir() / "tmp")
    # Node reserves more address space than the 2 GB call limit allows: its calls run with 8 GB.
    node = sandbox.run("node", {"node": node_bin(), "app_dir": "/app", "mode": "call", "package_dir": "/nonexistent",
                                "spec": {"function": "x"}, "inputs": {}}, paths=[], timeout=60, memory_mb=8192,
                       scratch_root=jobs.imports_dir() / "tmp")
    starts = node.get("code") == "import_failed"  # it started, then (as intended) found no package to load
    return {"sandbox_mode": sandbox.mode(), "result": res,
            "node_in_sandbox": "starts" if starts else node.get("reason") or node.get("error")}


class PublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=3, max_length=100, description="Cognex operation name, e.g. geo.distance")
    summary: Optional[str] = Field(default=None, max_length=300, description="Public one-line description")
    when_to_use: Optional[str] = Field(default=None, max_length=500,
                                       description="When an app or AI builder should pick this operation")


class AdminImportRequest(ImportRequest):
    publish: Optional[PublishRequest] = Field(default=None, description="Publish for every customer once Ready")


def _publish_or_error(import_id, body: PublishRequest, request: Request):
    try:
        row = _call(jobs.publish, import_id, body.name, body.summary, body.when_to_use)
    except jobs.SubmitError as exc:
        return JSONResponse(status_code=exc.status, content={"error": exc.code, "message": exc.reason})
    return jobs.public_view(row, _base_url(request))


@router.post("/v1/admin/imports", dependencies=[Depends(require_operator)], include_in_schema=False, status_code=202)
def admin_submit(body: AdminImportRequest, request: Request):
    try:
        row, created = _call(jobs.submit, jobs.OPERATOR_OWNER, body.url, body.entry_point, body.retry)
    except jobs.SubmitError as exc:
        return JSONResponse(status_code=exc.status, content={"error": exc.code, "message": exc.reason})
    view = jobs.public_view(row, _base_url(request))
    if body.publish is not None:
        view = _publish_or_error(row["id"], body.publish, request)
        if isinstance(view, JSONResponse):
            return view
    return JSONResponse(status_code=202 if created else 200, content=jsonable_encoder(view))


@router.get("/v1/admin/imports", dependencies=[Depends(require_operator)], include_in_schema=False)
def admin_list(request: Request):
    base = _base_url(request)
    return {"imports": [{**jobs.public_view(r, base), "owner": r["customer_id"]} for r in _call(jobs.list_all)]}


@router.get("/v1/admin/imports/{import_id}", dependencies=[Depends(require_operator)], include_in_schema=False)
def admin_get(import_id: uuid.UUID, request: Request):
    row = _call(jobs.get_any, import_id)
    if row is None or (row["customer_id"] != jobs.OPERATOR_OWNER and not row["published_name"]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "import not found")  # customers' imports stay private
    return jobs.public_view(row, _base_url(request))


@router.post("/v1/admin/imports/{import_id}/publish", dependencies=[Depends(require_operator)],
             include_in_schema=False)
def admin_publish(import_id: uuid.UUID, body: PublishRequest, request: Request):
    return _publish_or_error(import_id, body, request)


@router.post("/v1/admin/imports/{import_id}/refresh-attribution", dependencies=[Depends(require_operator)],
             include_in_schema=False)
def admin_refresh_attribution(import_id: uuid.UUID):
    """Re-detect license + copyright from the import's pinned commit (attribution only)."""
    try:
        out = _call(jobs.refresh_attribution, import_id)
    except jobs.ImportFailure as exc:
        return JSONResponse(status_code=502, content={"error": exc.code, "message": exc.reason})
    if out is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "import not found")
    return out


class EntryPointChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entry_point: str = Field(min_length=1, max_length=220,
                             description="One of the candidates the import listed: a function, export, command or 'POST /path'")


@router.post("/v1/admin/imports/{import_id}/entry-point", dependencies=[Depends(require_operator)],
             include_in_schema=False)
def admin_choose_entry_point(import_id: uuid.UUID, body: EntryPointChoice):
    """Pick the entry point of an ambiguous repo: saved as its configuration and its failed imports re-run."""
    try:
        return jsonable_encoder(_call(jobs.choose_entry_point, import_id, body.entry_point))
    except jobs.SubmitError as exc:
        return JSONResponse(status_code=exc.status, content={"error": exc.code, "message": exc.reason})


@router.post("/v1/admin/imports/{import_id}/unpublish", dependencies=[Depends(require_operator)],
             include_in_schema=False)
def admin_unpublish(import_id: uuid.UUID, request: Request):
    row = _call(jobs.unpublish, import_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "import not found")
    return jobs.public_view(row, _base_url(request))
