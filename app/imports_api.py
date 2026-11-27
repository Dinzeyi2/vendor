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
from app.imports import jobs, sandbox

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
    entry_point: Optional[str] = Field(default=None, max_length=100,
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


@router.get("/v1/admin/imports/sandbox-selftest", dependencies=[Depends(require_operator)], include_in_schema=False)
def sandbox_selftest():
    """Runs Cognex's own probe inside the sandbox (no customer code) and reports what it could do."""
    res = sandbox.run("selftest", {"app_dir": "/app", "alloc_bytes": 3 * 1024 ** 3}, paths=[], timeout=60,
                      scratch_root=jobs.imports_dir() / "tmp")
    return {"sandbox_mode": sandbox.mode(), "result": res}


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


@router.post("/v1/admin/imports/{import_id}/unpublish", dependencies=[Depends(require_operator)],
             include_in_schema=False)
def admin_unpublish(import_id: uuid.UUID, request: Request):
    row = _call(jobs.unpublish, import_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "import not found")
    return jobs.public_view(row, _base_url(request))
