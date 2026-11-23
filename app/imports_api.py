"""HTTP endpoints for customer GitHub imports (website backend auth, scoped to the signed-in customer).

- POST /v1/account/imports          {"url": "https://github.com/OWNER/REPO", optional "entry_point", "retry"}
                                     202 queued (new) / 200 existing import of the same repo + commit
- GET  /v1/account/imports          this customer's imports, newest first
- GET  /v1/account/imports/{id}     one import: status, stage, failure reason, or (ready) the
                                     operation name, schema and a copyable POST /v1/execute example
- GET  /v1/admin/imports/sandbox-selftest   operator: prove what the sandbox blocks on this server
"""
from __future__ import annotations

import os
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app import db
from app.account_api import signed_in_customer
from app.billing_api import require_operator
from app.imports import jobs, sandbox

router = APIRouter()


def _base_url(request: Request) -> str:
    explicit = os.environ.get("COGNEX_PUBLIC_URL")
    if explicit:
        return explicit.rstrip("/")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost"
    proto = request.headers.get("x-forwarded-proto") or ("http" if host.startswith(("127.", "localhost")) else "https")
    return f"{proto}://{host}"


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
