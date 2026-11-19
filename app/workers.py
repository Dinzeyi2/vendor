"""Persistent worker subprocesses with per-call timeouts and crash isolation.

Each library runs in its own long-lived process so heavy imports (Torch, Paddle, the JVM,
Julia) happen once. A call that times out or crashes kills only that worker; the next call
to the same library starts a fresh one.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional

from app.config import ROOT

log = logging.getLogger("uvicorn.error")


class WorkerFailure(Exception):
    def __init__(self, error_type: str, message: str):
        super().__init__(message)
        self.error_type = error_type


class WorkerProcess:
    def __init__(self, name: str, cmd: List[str], env: Optional[Dict[str, str]] = None,
                 cwd: str = str(ROOT)):
        self.name = name
        self.cmd = cmd
        self.env = env or {}
        self.cwd = cwd
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._lines: Optional[queue.Queue] = None
        self._seq = 0

    # -- lifecycle -----------------------------------------------------
    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _spawn(self, deadline: float) -> None:
        log.info("starting worker %s", self.name)
        self._proc = subprocess.Popen(self.cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=None, cwd=self.cwd, env={**os.environ, **self.env})
        self._lines = queue.Queue()
        threading.Thread(target=self._read, args=(self._proc, self._lines), daemon=True,
                         name=f"worker-{self.name}").start()
        if not self._next(deadline, "starting").get("ready"):
            raise WorkerFailure("worker_crashed", f"worker '{self.name}' sent no ready signal")

    @staticmethod
    def _read(proc: subprocess.Popen, lines: queue.Queue) -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            lines.put(line)
        lines.put(None)  # EOF: the process exited

    def kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            log.warning("killing worker %s", self.name)
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass

    def _next(self, deadline: float, phase: str) -> dict:
        remaining = deadline - time.monotonic()
        try:
            if remaining <= 0:
                raise queue.Empty
            line = self._lines.get(timeout=remaining)  # type: ignore[union-attr]
        except queue.Empty:
            raise WorkerFailure("timeout", f"worker '{self.name}' timed out while {phase}") from None
        if line is None:
            code = self._proc.wait() if self._proc else None
            raise WorkerFailure("worker_crashed", f"worker '{self.name}' exited (code {code}) while {phase}")
        return json.loads(line)

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
                self._proc.stdin.write((json.dumps({"id": rid, "op": op, "inputs": inputs}) + "\n").encode())
                self._proc.stdin.flush()
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
