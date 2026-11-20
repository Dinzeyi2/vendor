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
from typing import Any, Callable, Dict, List, Literal, Optional

from fastapi import Depends, FastAPI, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, field_validator

from app import accounts, billing, catalog, db
from app.account_api import router as account_router
from app.billing_api import error_response, router as billing_router
from app.errors import OperationError
from app.registry import OPERATIONS, WORKERS, lookup, warm_list
from app.workers import WorkerFailure

log = logging.getLogger("uvicorn.error")
MAX_TASKS = 32
# Threads that wait on workers; each blocks only while its task runs.
_executor = ThreadPoolExecutor(max_workers=64, thread_name_prefix="task")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _executor
    loop = asyncio.get_running_loop()
    for name in warm_list():  # optional pre-start, in the background so /health is up immediately
        loop.run_in_executor(_executor, _warm, name)
    maintenance = None
    if db.configured():  # migrate at boot; failures are retried on first use
        loop.run_in_executor(_executor, _init_db)
        maintenance = billing.Maintenance()  # heartbeat + release reservations of dead API processes
        maintenance.start()
    yield
    if maintenance is not None:
        maintenance.stop.set()
    for worker in WORKERS.values():
        worker.kill()
    # Swap in a fresh pool so the app object stays usable if it is started again (tests).
    old, _executor = _executor, ThreadPoolExecutor(max_workers=64, thread_name_prefix="task")
    old.shutdown(wait=False, cancel_futures=True)
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


app = FastAPI(title="Cognex API", version="0.5.0", lifespan=lifespan)
app.include_router(account_router)
app.include_router(billing_router)

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
    operation: str = Field(description="A Cognex operation name from GET /v1/operations, e.g. text.similarity")
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


def optional_caller(provided: Optional[str] = Security(_api_key_header)) -> Optional[Caller]:
    """No key: public view. A key, if sent, must be valid (so a typo is not silently ignored)."""
    return require_api_key(provided) if provided else None


@app.get("/v1/operations")
def list_operations(caller: Optional[Caller] = Depends(optional_caller)) -> Dict[str, Any]:
    """Public catalog of Cognex operations. Compatibility aliases are accepted but not listed."""
    operator = caller is not None and caller.kind == "operator"
    ops = []
    for name, spec in OPERATIONS.items():
        meta = catalog.BY_NAME.get(name)
        item: Dict[str, Any] = {"operation": name, "summary": meta.summary if meta else "",
                                "credits": catalog.CREDITS_PER_OPERATION,
                                "default_timeout_s": spec.default_timeout, "max_timeout_s": spec.max_timeout}
        if operator:  # aliases and worker internals are for the operator only
            item.update({
                "aliases": [a for a, n in catalog.ALIASES.items() if n == name],
                "runs_in": "api" if spec.inline else f"worker:{spec.worker}",
                "worker_running": bool(spec.worker and WORKERS[spec.worker].alive),
                "worker_unexpected_exits": WORKERS[spec.worker].exits if spec.worker else 0,
                "worker_notes": list(WORKERS[spec.worker].notes) if spec.worker else []})
        ops.append(item)
    return {"credits_per_operation": catalog.CREDITS_PER_OPERATION, "operations": ops,
            "open_source_acknowledgements": "/v1/open-source"}


@app.get("/v1/open-source")
def open_source() -> Dict[str, Any]:
    """Public: the open-source projects behind the operations, with their licenses (attribution page)."""
    return {"projects": [
        {"operation": e.name, "project": e.built_on.project, "license": e.built_on.license,
         "copyright": e.built_on.copyright, "source": e.built_on.source} for e in catalog.ENTRIES]}


_STDERR_MARKER = "; last stderr:"


async def _run(task: Task, diagnostics: bool, on_start: Optional[Callable[[], None]] = None) -> TaskResult:
    started = time.perf_counter()

    def result(**kw) -> TaskResult:
        return TaskResult(id=task.id, operation=task.operation,
                          duration_ms=round((time.perf_counter() - started) * 1000, 1), **kw)

    found = lookup(task.operation)
    if found is None:
        return result(status="error", error_type="unknown_operation",
                      error=f"unknown operation '{task.operation}'; see GET /v1/operations")
    public, spec = found
    timeout = min(task.timeout_s or spec.default_timeout, spec.max_timeout)
    loop = asyncio.get_running_loop()
    try:
        if spec.inline is not None:
            def run_inline():
                if on_start is not None:
                    on_start()
                return spec.inline(task.inputs)
            output = await asyncio.wait_for(loop.run_in_executor(_executor, run_inline), timeout)
        else:
            output = await loop.run_in_executor(_executor, WORKERS[spec.worker].call,
                                                spec.internal or public, task.inputs, timeout, on_start)
        if task.operation == public:  # engine-specific fields are only kept for the original names
            for key in catalog.ENGINE_FIELDS.get(public, ()):
                output.pop(key, None)
        return result(status="ok", output=output)
    except OperationError as exc:
        return result(status="error", error_type="invalid_input", error=str(exc))
    except WorkerFailure as exc:
        message = str(exc)
        if not diagnostics:  # worker stderr is shared across requests: never show it to customers
            message = message.split(_STDERR_MARKER, 1)[0]
            if spec.internal:  # name the operation the way the customer knows it
                message = message.replace(spec.internal, public)
        return result(status="error", error_type=exc.error_type, error=message)
    except asyncio.TimeoutError:
        return result(status="error", error_type="timeout", error=f"operation exceeded {timeout:g}s")
    except Exception as exc:  # never let one task fail the whole request
        log.exception("task %s failed", task.id)
        return result(status="error", error_type="operation_failed", error=f"{type(exc).__name__}: {exc}")


@app.post("/v1/execute", response_model=ExecuteResponse, response_model_exclude_none=True,
          responses={402: {"description": "insufficient_credits: nothing was run"}})
async def execute(request: ExecuteRequest, caller: Caller = Depends(require_api_key)):
    """Run all tasks concurrently; tasks for the same library are queued on that library's worker.

    Customer keys (when BILLING_MODE=on): 1 credit per known operation (public name or alias). The full batch
    cost is reserved atomically before any work starts (402 insufficient_credits otherwise);
    operations that never start are refunded. Unknown operations are rejected free of charge.
    Calls made with a customer key are recorded as usage. The operator key is never charged.
    """
    started = time.perf_counter()
    loop = asyncio.get_running_loop()
    reservation: Optional[billing.Reservation] = None
    known = [(t, found[1]) for t in request.tasks if (found := lookup(t.operation))]
    cost = len(known) * catalog.CREDITS_PER_OPERATION
    if caller.owner is not None and cost and billing.enforced():
        max_seconds = sum(min(t.timeout_s or spec.default_timeout, spec.max_timeout) for t, spec in known) + 120
        try:
            reservation = await loop.run_in_executor(_executor, billing.reserve, caller.owner, cost, max_seconds)
        except billing.BillingError as exc:
            return error_response(exc)
        except db.DatabaseUnavailable:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="credit store unavailable") from None

    charged = 0
    try:
        on_start = reservation.mark_started if reservation else None
        results = list(await asyncio.gather(*(_run(t, caller.kind == "operator", on_start) for t in request.tasks)))
    finally:
        if reservation is not None:  # charge started operations, release the rest (also on errors)
            try:
                charged = await loop.run_in_executor(_executor, billing.settle, reservation)
            except Exception as exc:  # the sweeper settles it from the persisted start count
                log.error("settling reservation %s failed: %s", reservation.id, type(exc).__name__)
                charged = reservation.started
    if caller.owner is not None:
        await _record_usage(caller.owner, (time.perf_counter() - started) * 1000, results, charged)
    return ExecuteResponse(results=results)


async def _record_usage(owner: accounts.KeyOwner, duration_ms: float, results: List[TaskResult],
                        credits_charged: int) -> None:
    # Only the operation's public name (registered names only), outcome and timing are recorded.
    entries = [{"operation": found[0] if (found := lookup(r.operation)) else "unknown",
                "status": r.status, "error_type": r.error_type, "duration_ms": r.duration_ms}
               for r in results]
    try:
        await asyncio.get_running_loop().run_in_executor(
            _executor, accounts.record_usage, owner, round(duration_ms, 1), entries, credits_charged)
    except Exception as exc:  # the work is done; don't fail the response, but make it visible
        log.error("usage recording failed for key %s: %s", owner.key_id, type(exc).__name__)
