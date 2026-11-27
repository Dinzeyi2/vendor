"""Long-running operations: POST /v1/execute answers `status: "queued"` with a job_id, the work runs
in the background, and GET /v1/jobs/{job_id} returns the result with the same API.

A task runs as a job when its operation is marked long_running or the task sets "async": true.
Billing is the same as for any task: 1 credit per operation that starts, reserved when the job is
accepted (402 insufficient_credits otherwise) and settled when it finishes (refunded if it never
starts). Inputs are never stored; results are kept for a day and are visible only to the same
customer (any of its keys) or, for operator jobs, the operator key.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Awaitable, Callable, Dict, Optional

from app import billing, db

log = logging.getLogger("uvicorn.error")
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="cognex-job")
_lock = threading.Lock()


def create(customer_id: Optional[str], key_id: Optional[uuid.UUID], operation: str) -> uuid.UUID:
    job_id = uuid.uuid4()
    with db.pool().connection() as conn:
        conn.execute("INSERT INTO execution_jobs (id, customer_id, key_id, operation) VALUES (%s, %s, %s, %s)",
                     (job_id, customer_id, key_id, operation))
    return job_id


def _set(job_id: uuid.UUID, **fields) -> None:
    sets = ", ".join(f"{k} = %s" for k in fields)
    vals = [json.dumps(v) if k == "output" and v is not None else v for k, v in fields.items()]
    with db.pool().connection() as conn:
        conn.execute(f"UPDATE execution_jobs SET {sets} WHERE id = %s", (*vals, job_id))


def start(job_id: uuid.UUID, run: Callable[[Optional[Callable[[], None]]], Awaitable[Any]],
          reservation: Optional["billing.Reservation"], record: Callable[[Any, int], None]) -> None:
    """Run `run(on_start)` (the normal task path) in the background and store its TaskResult."""
    def work() -> None:
        charged = 0
        result = None
        try:
            def on_start() -> None:
                _set(job_id, status="running", started_at=_now())
                if reservation is not None:
                    reservation.mark_started()
            result = asyncio.run(run(on_start))
            _set(job_id, status="ok" if result.status == "ok" else "error", output=result.output,
                 error_type=result.error_type, error=result.error, finished_at=_now())
        except Exception as exc:  # noqa: BLE001 - never leave a job running
            log.exception("job %s crashed", job_id)
            _set(job_id, status="error", error_type="operation_failed", error=f"{type(exc).__name__}",
                 finished_at=_now())
        finally:
            if reservation is not None:
                try:
                    charged = billing.settle(reservation)
                except Exception as exc:  # noqa: BLE001 - the sweeper settles it from the start count
                    log.error("settling job %s failed: %s", job_id, type(exc).__name__)
                    charged = reservation.started
            try:
                _set(job_id, credits_charged=charged)
                if result is not None:
                    record(result, charged)
            except Exception as exc:  # noqa: BLE001
                log.error("recording job %s failed: %s", job_id, type(exc).__name__)
    _pool.submit(work)


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def get(job_id: uuid.UUID, customer_id: Optional[str]) -> Optional[Dict[str, Any]]:
    with db.pool().connection() as conn:
        conn.execute("DELETE FROM execution_jobs WHERE expires_at < now()")
        r = conn.execute("SELECT id, operation, status, output, error_type, error, credits_charged, created_at, "
                         "started_at, finished_at, expires_at FROM execution_jobs WHERE id = %s AND "
                         "customer_id IS NOT DISTINCT FROM %s", (job_id, customer_id)).fetchone()
    if r is None:
        return None
    keys = ("job_id", "operation", "status", "output", "error_type", "error", "credits_charged", "created_at",
            "started_at", "finished_at", "expires_at")
    out = {k: v for k, v in zip(keys, r) if v is not None}
    out["job_id"] = str(out["job_id"])
    return out


def mark_lost() -> None:
    """At startup: jobs that were running in a previous process can't finish; say so."""
    with db.pool().connection() as conn:
        conn.execute("UPDATE execution_jobs SET status = 'error', error_type = 'worker_crashed', error = 'the server "
                     "restarted before the job finished; submit it again', finished_at = now() WHERE status IN "
                     "('queued', 'running')")
