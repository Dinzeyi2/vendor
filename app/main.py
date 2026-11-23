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

from fastapi import Depends, FastAPI, HTTPException, Request, Response, Security, status
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, field_validator, model_validator

from app import accounts, billing, catalog, db, discovery, router
from app.imports import jobs as import_jobs, private as private_imports
from app.account_api import router as account_router
from app.billing_api import error_response, router as billing_router
from app.errors import OperationError
from app.registry import CAPABILITIES, OPERATIONS, WORKERS, lookup, warm_list
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
    maintenance = importer = None
    if db.configured():  # migrate at boot; failures are retried on first use
        loop.run_in_executor(_executor, _init_db)
        maintenance = billing.Maintenance()  # heartbeat + release reservations of dead API processes
        maintenance.start()
        if import_jobs.enabled():
            importer = import_jobs.ImportWorker()  # customer GitHub imports, one at a time
            importer.start()
    yield
    if maintenance is not None:
        maintenance.stop.set()
    if importer is not None:
        importer.stop.set()
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
from app.imports_api import router as imports_router  # noqa: E402
app.include_router(imports_router)

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
    """Either name an operation (`operation` + `inputs`) or describe the goal in plain English
    (`request` + `data`) and let Cognex pick the matching registered operation."""
    id: str
    operation: Optional[str] = Field(default=None, description="A Cognex operation name from GET /v1/operations, "
                                                               "e.g. text.similarity")
    inputs: Dict[str, Any] = Field(default_factory=dict)
    request: Optional[str] = Field(default=None, min_length=1, max_length=router.MAX_REQUEST_CHARS,
                                   description="Plain-English goal, e.g. 'Compare these two names' (instead of operation)")
    data: Any = Field(default=None, description="The data for a plain-English request, in any reasonable shape")
    timeout_s: Optional[float] = Field(default=None, gt=0, description="Override the operation's default timeout (capped).")

    @model_validator(mode="after")
    def operation_or_request(self) -> "Task":
        if (self.operation is None) == (self.request is None):
            raise ValueError("give either 'operation' (with 'inputs') or 'request' (with 'data'), not both")
        if self.request is not None and self.data is None and self.inputs:
            self.data = self.inputs  # plain-English request that put its data in 'inputs'
        return self


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
    operation: Optional[str] = None   # the operation that ran (for plain-English requests: the one chosen)
    status: Literal["ok", "error", "needs_clarification"]
    output: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    error_type: Optional[ErrorType] = None
    question: Optional[str] = None    # needs_clarification: one short question; nothing ran or was charged
    options: Optional[List[str]] = None
    routing: Optional[Dict[str, Any]] = None  # plain-English requests: why this operation was chosen
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


def _published_safe() -> Dict[str, Any]:
    try:
        return private_imports.published() if db.configured() else {}
    except db.DatabaseUnavailable:
        return {}


def _discoverable(caller: Optional[Caller], base: str) -> List[Dict[str, Any]]:
    """Every operation this caller can run: built-in, published, then the caller's own imports."""
    operator = caller is not None and caller.kind == "operator"
    ops = []
    for name, spec in OPERATIONS.items():
        item = discovery.describe(CAPABILITIES[name], base)
        if operator:  # aliases and worker internals are for the operator only
            item.update({
                "aliases": [a for a, n in catalog.ALIASES.items() if n == name],
                "runs_in": "api" if spec.inline else f"worker:{spec.worker}",
                "worker_running": bool(spec.worker and WORKERS[spec.worker].alive),
                "worker_unexpected_exits": WORKERS[spec.worker].exits if spec.worker else 0,
                "worker_notes": list(WORKERS[spec.worker].notes) if spec.worker else []})
        ops.append(item)
    published = _published_safe()
    ops += [discovery.describe(cap, base) for cap, _ in published.values()]
    if caller is not None and caller.owner is not None and caller.owner.kind == "customer":
        ops += [discovery.describe(cap, base, private=True) for name, (cap, _)
                in private_imports.capabilities_for(caller.owner.customer_id).items() if name not in published]
    return ops


@app.get("/v1/operations")
def list_operations(request: Request, response: Response,
                    caller: Optional[Caller] = Depends(optional_caller)):
    """Discovery for apps and AI app builders: every operation you can run, with its purpose, when to
    use it, required inputs, optional settings, and a working POST /v1/execute request and response.

    Built-in and published operations are listed for everyone; with a customer key, that customer's
    own private imports are added. Newly published operations appear within seconds.
    `catalog_version` (also the ETag) changes whenever the list changes; send If-None-Match to get
    304 when nothing changed. Compatibility aliases are accepted by /v1/execute but not listed.
    """
    base = discovery.base_url(request)
    ops = _discoverable(caller, base)
    ver = discovery.version(ops)
    headers = {"ETag": f'"{ver}"', "Cache-Control": "no-cache"}
    if request.headers.get("if-none-match", "").strip('W/ "') == ver:
        return Response(status_code=304, headers=headers)
    response.headers.update(headers)
    return {"catalog_version": ver, "count": len(ops), "credits_per_operation": catalog.CREDITS_PER_OPERATION,
            "how_to_call": discovery.how_to_call(base, MAX_TASKS), "operations": ops,
            "open_source_acknowledgements": "/v1/open-source"}


@app.get("/v1/operations/{name}")
def get_operation(name: str, request: Request, caller: Optional[Caller] = Depends(optional_caller)):
    """One operation's discovery entry (by its name or a compatibility alias)."""
    base = discovery.base_url(request)
    found = discovery.find(_discoverable(caller, base), name)
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown operation '{name}'; see GET /v1/operations")
    return {**found, "how_to_call": discovery.how_to_call(base, MAX_TASKS)}


@app.get("/v1/open-source")
def open_source() -> Dict[str, Any]:
    """Public: the open-source projects behind the operations, with their licenses (attribution page)."""
    projects = [{"operation": e.name, "project": e.built_on.project, "license": e.built_on.license,
                 "copyright": e.built_on.copyright, "source": e.built_on.source} for e in catalog.ENTRIES]
    published = _published_safe()
    projects += [{"operation": name, "project": cap.built_on.project, "license": cap.built_on.license,
                  "copyright": cap.built_on.copyright, "source": cap.built_on.source}
                 for name, (cap, _) in published.items()]
    return {"projects": projects}


_STDERR_MARKER = "; last stderr:"


@dataclass
class _Plan:
    """A task after routing and input validation, before anything runs or is charged."""
    task: Task
    echo: Optional[str] = None          # operation name to report (what the caller sent, or the chosen one)
    public: Optional[str] = None
    spec: Any = None
    inputs: Optional[Dict[str, Any]] = None
    routing: Optional[Dict[str, Any]] = None
    done: Optional[Dict[str, Any]] = None  # answered without running: error or clarification (free)

    @property
    def runnable(self) -> bool:
        return self.done is None


def _plan(task: Task, private: Optional[Dict[str, Any]] = None) -> _Plan:
    private = private or {}
    if task.operation is not None:
        found = lookup(task.operation)
        if found is None and task.operation in private:  # a published import, or the caller's own
            cap, spec = private[task.operation]
            problem = router.validation_error(cap, task.inputs)
            if problem:
                return _Plan(task, echo=task.operation, done={"status": "error", "error_type": "invalid_input",
                                                              "error": problem})
            return _Plan(task, echo=task.operation, public=task.operation, spec=spec, inputs=task.inputs)
        if found is None:
            return _Plan(task, echo=task.operation, done={
                "status": "error", "error_type": "unknown_operation",
                "error": f"unknown operation '{task.operation}'; see GET /v1/operations"})
        public, spec = found
        cap = CAPABILITIES.get(public)
        problem = router.validation_error(cap, task.inputs) if cap else None
        if problem:  # rejected before running, so not charged
            return _Plan(task, echo=task.operation, done={"status": "error", "error_type": "invalid_input",
                                                          "error": problem})
        return _Plan(task, echo=task.operation, public=public, spec=spec, inputs=task.inputs)

    caps = list(CAPABILITIES.values()) + [cap for cap, _ in private.values()]
    decision = router.route(task.request, task.data, caps)
    if not decision.runnable:
        return _Plan(task, echo=decision.capability.name if decision.capability else None, done={
            "status": "needs_clarification", "question": decision.question, "options": decision.options})
    name = decision.capability.name
    spec = OPERATIONS[name] if name in OPERATIONS else private[name][1]
    return _Plan(task, echo=name, public=name, spec=spec, inputs=decision.inputs,
                 routing={"matched": decision.matched, "confidence": decision.confidence})


async def _run(plan: _Plan, diagnostics: bool, on_start: Optional[Callable[[], None]] = None) -> TaskResult:
    started = time.perf_counter()
    task = plan.task

    def result(**kw) -> TaskResult:
        return TaskResult(id=task.id, operation=plan.echo, routing=plan.routing,
                          duration_ms=round((time.perf_counter() - started) * 1000, 1), **kw)

    if plan.done is not None:
        return result(**plan.done)
    public, spec, inputs = plan.public, plan.spec, plan.inputs
    timeout = min(task.timeout_s or spec.default_timeout, spec.max_timeout)
    loop = asyncio.get_running_loop()
    try:
        if spec.runner is not None:  # customer import: a fresh sandbox per call
            output = await loop.run_in_executor(_executor, spec.runner, inputs, timeout, on_start)
        elif spec.inline is not None:
            def run_inline():
                if on_start is not None:
                    on_start()
                return spec.inline(inputs)
            output = await asyncio.wait_for(loop.run_in_executor(_executor, run_inline), timeout)
        else:
            output = await loop.run_in_executor(_executor, WORKERS[spec.worker].call,
                                                spec.internal or public, inputs, timeout, on_start)
        if plan.echo == public:  # engine-specific fields are only kept for the original names
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

    A task names an operation, or gives a plain-English `request` plus `data`; Cognex then picks
    a registered operation or answers with one short question (status needs_clarification).
    Every task is routed and its inputs validated before anything runs.

    Customer keys (when BILLING_MODE=on): 1 credit per operation that will run. The full batch
    cost is reserved atomically before any work starts (402 insufficient_credits otherwise);
    operations that never start are refunded. Clarifications, unknown operations and inputs that
    fail validation are free. Calls made with a customer key are recorded as usage. The operator
    key is never charged.
    """
    started = time.perf_counter()
    loop = asyncio.get_running_loop()
    reservation: Optional[billing.Reservation] = None
    owner = caller.owner
    private: Dict[str, Any] = {}   # imported operations this caller may use (published + its own)
    checking = owner is not None and owner.kind == "import_check"
    if checking or any(t.request is not None or lookup(t.operation) is None for t in request.tasks):
        def load_imports() -> Dict[str, Any]:
            if checking:
                return private_imports.capabilities_for(owner.customer_id, owner.scope)
            own = private_imports.capabilities_for(owner.customer_id) if owner is not None else {}
            return {**own, **(private_imports.published() if db.configured() else {})}
        try:
            private = await loop.run_in_executor(_executor, load_imports)
        except db.DatabaseUnavailable:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="key store unavailable") from None
    if checking:  # Cognex's verification key: its one import only
        if any(t.operation not in private for t in request.tasks):
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="this key may only call the import it verifies")
    plans = await loop.run_in_executor(_executor, lambda: [_plan(t, private) for t in request.tasks])
    runnable = [p for p in plans if p.runnable]
    cost = len(runnable) * catalog.CREDITS_PER_OPERATION
    if owner is not None and owner.kind == "customer" and cost and billing.enforced():
        max_seconds = sum(min(p.task.timeout_s or p.spec.default_timeout, p.spec.max_timeout) for p in runnable) + 120
        try:
            reservation = await loop.run_in_executor(_executor, billing.reserve, caller.owner, cost, max_seconds)
        except billing.BillingError as exc:
            return error_response(exc)
        except db.DatabaseUnavailable:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="credit store unavailable") from None

    charged = 0
    try:
        on_start = reservation.mark_started if reservation else None
        results = list(await asyncio.gather(*(_run(p, caller.kind == "operator", on_start) for p in plans)))
    finally:
        if reservation is not None:  # charge started operations, release the rest (also on errors)
            try:
                charged = await loop.run_in_executor(_executor, billing.settle, reservation)
            except Exception as exc:  # the sweeper settles it from the persisted start count
                log.error("settling reservation %s failed: %s", reservation.id, type(exc).__name__)
                charged = reservation.started
    if owner is not None and owner.kind == "customer":
        await _record_usage(owner, (time.perf_counter() - started) * 1000, results, charged, private)
    return ExecuteResponse(results=results)


def _usage_name(r: TaskResult, private: Dict[str, Any]) -> str:
    if r.operation and r.operation in private:
        return r.operation
    if r.status == "needs_clarification" and (r.operation is None or lookup(r.operation) is None):
        return "router"
    found = lookup(r.operation) if r.operation else None
    return found[0] if found else "unknown"


async def _record_usage(owner: accounts.KeyOwner, duration_ms: float, results: List[TaskResult],
                        credits_charged: int, private: Optional[Dict[str, Any]] = None) -> None:
    # Only the operation's public name (registered names only), outcome and timing are recorded:
    # never inputs, data, plain-English requests, outputs or questions.
    entries = [{"operation": _usage_name(r, private or {}), "status": r.status, "error_type": r.error_type,
                "duration_ms": r.duration_ms} for r in results]
    try:
        await asyncio.get_running_loop().run_in_executor(
            _executor, accounts.record_usage, owner, round(duration_ms, 1), entries, credits_charged)
    except Exception as exc:  # the work is done; don't fail the response, but make it visible
        log.error("usage recording failed for key %s: %s", owner.key_id, type(exc).__name__)
