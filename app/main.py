"""Unified algorithm API."""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, field_validator

from app import accounts, db
from app.account_api import router as account_router
from app.errors import OperationError
from app.registry import OPERATIONS, WORKERS, warm_list
from app.workers import WorkerFailure

log = logging.getLogger("uvicorn.error")
MAX_TASKS = 32
# Threads that wait on workers; each blocks only while its task runs.
_executor = ThreadPoolExecutor(max_workers=64, thread_name_prefix="task")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    loop = asyncio.get_running_loop()
    for name in warm_list():  # optional pre-start, in the background so /health is up immediately
        loop.run_in_executor(_executor, _warm, name)
    if db.configured():  # create the schema at boot; failures are retried on first use
        loop.run_in_executor(_executor, _init_db)
    yield
    for worker in WORKERS.values():
        worker.kill()
    _executor.shutdown(wait=False, cancel_futures=True)
    db.close()


def _init_db() -> None:
    try:
        db.pool()
    except db.DatabaseUnavailable as exc:
        log.error("database init failed (will retry on first use): %s", exc)


def _warm(name: str) -> None:
    try:
        WORKERS[name].warm(timeout=900)
        log.info("worker %s warmed", name)
    except Exception as exc:
        log.error("worker %s failed to warm: %s", name, exc)


app = FastAPI(title="Unified Algorithm API", version="0.3.0", lifespan=lifespan)
app.include_router(account_router)

API_KEY_ENV = "API_KEY"
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


@dataclass(frozen=True)
class Caller:
    """Who is calling the algorithm endpoints: the operator (API_KEY) or a customer key."""
    kind: Literal["operator", "customer"]
    owner: Optional[accounts.KeyOwner] = None


def require_api_key(provided: Optional[str] = Security(_api_key_header)) -> Caller:
    """Accept the operator API_KEY or an active customer key in X-API-Key.

    Customer keys are checked against Postgres on every call, so a revoked key fails on its
    next request. Fails closed: with neither API_KEY nor a database configured, returns 503.
    """
    expected = os.environ.get(API_KEY_ENV)
    if not expected and not db.configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="API key not configured on server")
    if provided and expected and secrets.compare_digest(provided.encode(), expected.encode()):
        return Caller("operator")
    if provided and db.configured() and accounts.looks_like_customer_key(provided):
        try:
            owner = accounts.authenticate_key(provided)
        except db.DatabaseUnavailable:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="key store unavailable") from None
        if owner is not None:
            return Caller("customer", owner)
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid, revoked or missing API key",
                        headers={"WWW-Authenticate": "APIKey"})


class Task(BaseModel):
    id: str
    operation: str
    inputs: Dict[str, Any] = Field(default_factory=dict)
    timeout_s: Optional[float] = Field(default=None, gt=0, description="Override the operation's default timeout (capped).")


class ExecuteRequest(BaseModel):
    tasks: List[Task] = Field(min_length=1, max_length=MAX_TASKS)

    @field_validator("tasks")
    @classmethod
    def unique_ids(cls, tasks: List[Task]) -> List[Task]:
        if len({t.id for t in tasks}) != len(tasks):
            raise ValueError("task ids must be unique within a request")
        return tasks


ErrorType = Literal["unknown_operation", "invalid_input", "operation_failed", "timeout", "worker_crashed"]


class TaskResult(BaseModel):
    id: str
    operation: str
    status: Literal["ok", "error"]
    output: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    error_type: Optional[ErrorType] = None
    duration_ms: float


class ExecuteResponse(BaseModel):
    results: List[TaskResult]


@app.get("/health")
def health() -> Dict[str, str]:
    # RAILWAY_GIT_COMMIT_SHA is set by Railway for GitHub deploys; lets callers confirm what is live.
    return {"status": "ok", "commit": (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or "unknown")[:7]}


@app.get("/v1/operations")
def list_operations(caller: Caller = Depends(require_api_key)) -> Dict[str, Any]:
    ops = []
    for name, spec in OPERATIONS.items():
        item = {"operation": name, "library": spec.library,
                "default_timeout_s": spec.default_timeout, "max_timeout_s": spec.max_timeout}
        if caller.kind == "operator":  # worker internals are for the operator only
            item.update({
                "runs_in": "api" if spec.inline else f"worker:{spec.worker}",
                "worker_running": bool(spec.worker and WORKERS[spec.worker].alive),
                "worker_unexpected_exits": WORKERS[spec.worker].exits if spec.worker else 0,
                "worker_notes": list(WORKERS[spec.worker].notes) if spec.worker else []})
        ops.append(item)
    return {"operations": ops}


_STDERR_MARKER = "; last stderr:"


async def _run(task: Task, diagnostics: bool) -> TaskResult:
    started = time.perf_counter()

    def result(**kw) -> TaskResult:
        return TaskResult(id=task.id, operation=task.operation,
                          duration_ms=round((time.perf_counter() - started) * 1000, 1), **kw)

    spec = OPERATIONS.get(task.operation)
    if spec is None:
        return result(status="error", error_type="unknown_operation",
                      error=f"unknown operation '{task.operation}'")
    timeout = min(task.timeout_s or spec.default_timeout, spec.max_timeout)
    loop = asyncio.get_running_loop()
    try:
        if spec.inline is not None:
            output = await asyncio.wait_for(loop.run_in_executor(_executor, spec.inline, task.inputs), timeout)
        else:
            output = await loop.run_in_executor(_executor, WORKERS[spec.worker].call,
                                                task.operation, task.inputs, timeout)
        return result(status="ok", output=output)
    except OperationError as exc:
        return result(status="error", error_type="invalid_input", error=str(exc))
    except WorkerFailure as exc:
        message = str(exc)
        if not diagnostics:  # worker stderr is shared across requests: never show it to customers
            message = message.split(_STDERR_MARKER, 1)[0]
        return result(status="error", error_type=exc.error_type, error=message)
    except asyncio.TimeoutError:
        return result(status="error", error_type="timeout", error=f"operation exceeded {timeout:g}s")
    except Exception as exc:  # never let one task fail the whole request
        log.exception("task %s failed", task.id)
        return result(status="error", error_type="operation_failed", error=f"{type(exc).__name__}: {exc}")


@app.post("/v1/execute", response_model=ExecuteResponse, response_model_exclude_none=True)
async def execute(request: ExecuteRequest, caller: Caller = Depends(require_api_key)) -> ExecuteResponse:
    """Run all tasks concurrently; tasks for the same library are queued on that library's worker.

    Calls made with a customer key are recorded as usage: one entry for the request and one per task.
    """
    started = time.perf_counter()
    results = list(await asyncio.gather(*(_run(t, caller.kind == "operator") for t in request.tasks)))
    if caller.owner is not None:
        await _record_usage(caller.owner, (time.perf_counter() - started) * 1000, results)
    return ExecuteResponse(results=results)


async def _record_usage(owner: accounts.KeyOwner, duration_ms: float, results: List[TaskResult]) -> None:
    # Only the operation name (registered names only), outcome and timing are recorded.
    entries = [{"operation": r.operation if r.operation in OPERATIONS else "unknown",
                "status": r.status, "error_type": r.error_type, "duration_ms": r.duration_ms}
               for r in results]
    try:
        await asyncio.get_running_loop().run_in_executor(
            _executor, accounts.record_usage, owner, round(duration_ms, 1), entries)
    except Exception as exc:  # the work is done; don't fail the response, but make it visible
        log.error("usage recording failed for key %s: %s", owner.key_id, type(exc).__name__)
