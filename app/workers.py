"""Persistent worker subprocesses with per-call timeouts and crash isolation.

Each library runs in its own long-lived process so heavy imports (Torch, Paddle, the JVM,
Julia) happen once. A call that times out or crashes kills only that worker; the next call
to the same library starts a fresh one.

stdout is reserved for the JSON-lines protocol. stderr is captured line by line, redacted,
and forwarded to the API's log (Railway's log stream). When a worker exits on its own, its
exit code, any exit reason it reported and its last stderr lines are logged.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
from typing import Any, Deque, Dict, List, Optional

from app.config import ROOT

log = logging.getLogger("uvicorn.error")

STDERR_TAIL_LINES = 40
MAX_LOG_LINE = 2000
# Env vars never passed to workers and whose values are redacted from forwarded logs.
# DATABASE*/PG* cover DATABASE_URL (it embeds the Postgres password) and libpq variables.
_SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH|DATABASE|^PG", re.IGNORECASE)


def _secret_values() -> List[str]:
    """Values of secret-looking environment variables (API_KEY etc.), longest first."""
    values = {v for k, v in os.environ.items() if _SECRET_NAME.search(k) and len(v) >= 8}
    return sorted(values, key=len, reverse=True)


def redact(text: str, secrets: Optional[List[str]] = None) -> str:
    for value in secrets if secrets is not None else _secret_values():
        text = text.replace(value, "[REDACTED]")
    return text


def worker_env(extra: Dict[str, str]) -> Dict[str, str]:
    """Environment for a worker: the API's environment minus secrets, plus worker settings."""
    env = {k: v for k, v in os.environ.items() if not _SECRET_NAME.search(k)}
    env.update(extra)
    return env


class WorkerFailure(Exception):
    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type


class _Run:
    """State for one spawned worker process."""

    def __init__(self, proc: subprocess.Popen):
        self.proc = proc
        self.lines: queue.Queue = queue.Queue()
        self.stderr_tail: Deque[str] = collections.deque(maxlen=STDERR_TAIL_LINES)
        self.stderr_done = threading.Event()
        self.exit_reason: Optional[str] = None
        self.killed = False


class WorkerProcess:
    def __init__(self, name: str, cmd: List[str], env: Optional[Dict[str, str]] = None,
                 cwd: str = str(ROOT)):
        self.name = name
        self.cmd = cmd
        self.env = env or {}
        self.cwd = cwd
        self._lock = threading.Lock()
        self._run: Optional[_Run] = None
        self._seq = 0
        self.notes: Deque[str] = collections.deque(maxlen=10)
        self.exits = 0  # unexpected exits since the API started

    def _add_note(self, note: str) -> None:
        note = redact(note)[:500]
        log.warning("worker %s note: %s", self.name, note)
        if note not in self.notes:
            self.notes.append(note)

    # -- lifecycle -----------------------------------------------------
    @property
    def alive(self) -> bool:
        return self._run is not None and self._run.proc.poll() is None

    def _spawn(self, deadline: float) -> None:
        log.info("starting worker %s", self.name)
        proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=self.cwd, env=worker_env(self.env))
        run = self._run = _Run(proc)
        secrets = _secret_values()
        for target, label in ((self._read_stdout, "out"), (self._read_stderr, "err"), (self._watch, "watch")):
            threading.Thread(target=target, args=(run, secrets) if label == "err" else (run,),
                             daemon=True, name=f"worker-{self.name}-{label}").start()
        if not self._next(deadline, "starting").get("ready"):
            raise WorkerFailure("worker_crashed", f"worker '{self.name}' sent no ready signal")

    def _read_stdout(self, run: _Run) -> None:
        for line in run.proc.stdout:  # type: ignore[union-attr]
            try:
                msg = json.loads(line)
            except ValueError:
                log.warning("[worker %s] non-protocol line on stdout ignored", self.name)
                continue
            if "fatal" in msg:  # the worker explains why it is about to exit
                run.exit_reason = str(msg["fatal"])[:500]
                continue
            if "note" in msg:  # diagnostic from the worker
                self._add_note(str(msg["note"]))
                continue
            for n in msg.get("notes") or []:
                self._add_note(str(n))
            run.lines.put(msg)
        run.lines.put(None)  # EOF: the process exited or closed stdout

    def _read_stderr(self, run: _Run, secrets: List[str]) -> None:
        try:
            for raw in run.proc.stderr:  # type: ignore[union-attr]
                line = redact(raw.decode("utf-8", "replace").rstrip(), secrets)[:MAX_LOG_LINE]
                if line:
                    run.stderr_tail.append(line)
                    log.info("[worker %s] %s", self.name, line)
        finally:
            run.stderr_done.set()

    def _watch(self, run: _Run) -> None:
        code = run.proc.wait()
        run.stderr_done.wait(timeout=5)
        if run.killed:
            log.info("worker %s stopped by the manager (exit code %s)", self.name, code)
            return
        self.exits += 1
        tail = "\n".join(f"    {l}" for l in run.stderr_tail) or "    (no stderr output)"
        log.error("worker %s exited unexpectedly with exit code %s; reason: %s; last stderr lines:\n%s",
                  self.name, code, run.exit_reason or "not reported", tail)

    def kill(self) -> None:
        run, self._run = self._run, None
        if run is not None and run.proc.poll() is None:
            run.killed = True
            log.warning("killing worker %s", self.name)
            run.proc.kill()
            try:
                run.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def _next(self, deadline: float, phase: str) -> dict:
        run = self._run
        remaining = deadline - time.monotonic()
        try:
            if remaining <= 0 or run is None:
                raise queue.Empty
            msg = run.lines.get(timeout=remaining)
        except queue.Empty:
            raise WorkerFailure("timeout", f"worker '{self.name}' timed out while {phase}") from None
        if msg is None:
            try:
                code = run.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                code = "still running, stdout closed"
            run.stderr_done.wait(timeout=2)
            reason = f"; reason: {run.exit_reason}" if run.exit_reason else "; reason: not reported"
            tail = " | ".join(list(run.stderr_tail)[-3:])[-600:]  # already redacted
            raise WorkerFailure("worker_crashed",
                                f"worker '{self.name}' exited (code {code}) while {phase}{reason}"
                                + (f"; last stderr: {tail}" if tail else ""))
        return msg

    # -- calls ---------------------------------------------------------
    def call(self, op: str, inputs: Dict[str, Any], timeout: float) -> Any:
        deadline = time.monotonic() + timeout
        if not self._lock.acquire(timeout=timeout):
            raise WorkerFailure("timeout", f"worker '{self.name}' stayed busy for {timeout:g}s")
        try:
            if not self.alive:
                self._spawn(deadline)
            self._seq += 1
            rid = self._seq
            try:
                self._run.proc.stdin.write((json.dumps({"id": rid, "op": op, "inputs": inputs}) + "\n").encode())
                self._run.proc.stdin.flush()
            except OSError as exc:
                raise WorkerFailure("worker_crashed", f"worker '{self.name}' is not accepting input: {exc}")
            msg = self._next(deadline, f"running {op}")
            while msg.get("id") != rid:  # skip anything stale
                msg = self._next(deadline, f"running {op}")
            if msg.get("ok"):
                return msg.get("output")
            raise WorkerFailure(msg.get("error_type", "operation_failed"), msg.get("error", "unknown error"))
        except WorkerFailure as exc:
            if exc.error_type in ("timeout", "worker_crashed"):
                self.kill()
            raise
        except Exception as exc:
            self.kill()
            raise WorkerFailure("worker_crashed", f"worker '{self.name}' failed: {exc}") from exc
        finally:
            self._lock.release()

    def warm(self, timeout: float) -> None:
        """Start the worker ahead of the first request (imports its library)."""
        with self._lock:
            if not self.alive:
                try:
                    self._spawn(time.monotonic() + timeout)
                except WorkerFailure:
                    self.kill()
                    raise
