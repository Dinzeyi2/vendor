"""Launch the isolated sandbox for imported (untrusted) code. Trusted code.

Every run is a fresh process (`python -I app/imports/sandbox_boot.py`) with:
- an empty environment: no API keys, secrets, DATABASE_URL or proxy settings
- a fresh scratch directory as cwd/HOME/TMPDIR, owned by a random unprivileged uid used only
  for this run (so concurrent runs, even of the same customer, can't see each other)
- the network blocked by seccomp (no AF_INET/AF_INET6 sockets; so no database either)
- limits on memory, CPU, processes, file size and open files, plus a wall-clock timeout that
  kills the whole process group

COGNEX_SANDBOX=strict (default) requires Linux x86-64 and starting as root; otherwise imports
fail with sandbox_unavailable. COGNEX_SANDBOX=insecure-dev skips the lockdown: local
development and tests only, never production.
"""
from __future__ import annotations

import json
import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import ROOT

BOOT = Path(__file__).resolve().with_name("sandbox_boot.py")
MAX_RESULT = 8 * 1024 * 1024


def mode() -> str:
    value = os.environ.get("COGNEX_SANDBOX", "strict").strip().lower()
    return "insecure-dev" if value == "insecure-dev" else "strict"


def _env(scratch: str) -> Dict[str, str]:
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": scratch, "TMPDIR": scratch, "TMP": scratch,
           "TEMP": scratch, "LANG": "C.UTF-8", "PYTHONHASHSEED": "0", "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONIOENCODING": "utf-8", "OMP_NUM_THREADS": "1", "OPENBLAS_NUM_THREADS": "1",
           "MKL_NUM_THREADS": "1", "NUMEXPR_NUM_THREADS": "1", "MPLBACKEND": "Agg"}
    if sys.platform == "win32":  # insecure-dev on Windows: the interpreter needs these to start
        for k in ("SYSTEMROOT", "WINDIR", "PATH"):
            if k in os.environ:
                env[k] = os.environ[k]
    return env


# Resource presets: quick calls, builds (compilers, package managers), dependency fetches.
PROFILES = {
    "call": {"memory_mb": 2048, "file_mb": 64, "open_files": 256, "processes": 64},
    "build": {"memory_mb": 6144, "file_mb": 1024, "open_files": 4096, "processes": 512},
    "fetch": {"memory_mb": 4096, "file_mb": 1024, "open_files": 4096, "processes": 256},
}


def _own(paths: List[Path], uid: int) -> None:
    """chown -R (and, for root, drop group/other write) so a build's files can't be changed later."""
    for top in paths:
        for dirpath, dirnames, filenames in os.walk(top):
            for name in [dirpath, *[os.path.join(dirpath, n) for n in dirnames + filenames]]:
                try:
                    os.lchown(name, uid, uid)
                    if uid == 0 and not os.path.islink(name):
                        os.chmod(name, os.stat(name).st_mode & ~0o022)
                except OSError:
                    pass


def run(sandbox_mode: str, payload: Dict[str, Any], *, paths: List[str], timeout: float,
        memory_mb: Optional[int] = None, cpu_seconds: Optional[int] = None, scratch_root: Optional[Path] = None,
        on_start=None, profile: str = "call", network: bool = False,
        writable: Optional[List[Path]] = None) -> Dict[str, Any]:
    """Run runner.MODES[sandbox_mode](payload) in the sandbox; returns its (untrusted) result dict.

    `writable` folders are handed to the run's uid for its duration (builds) and given back to root
    afterwards, read-only for everyone else. `network` is only for the dependency-fetch step."""
    uid = random.randint(20000, 59999)
    root = Path(scratch_root or tempfile.gettempdir())
    root.mkdir(parents=True, exist_ok=True)
    scratch = tempfile.mkdtemp(prefix="sbx-", dir=root)
    strict = mode() == "strict"
    owns = strict and hasattr(os, "chown") and os.geteuid() == 0
    prof = PROFILES[profile]
    try:
        if owns:
            os.chown(scratch, uid, uid)
            os.chmod(scratch, 0o700)
            _own([Path(p) for p in writable or []], uid)
        cfg = {"mode": sandbox_mode, "payload": {**payload, "scratch": scratch}, "uid": uid, "strict": strict,
               "app_dir": str(ROOT), "paths": [str(p) for p in paths], "network": bool(network),
               "limits": {"memory_bytes": (memory_mb or prof["memory_mb"]) * 1024 * 1024,
                          "cpu_seconds": int(cpu_seconds or timeout + 5),
                          "file_bytes": prof["file_mb"] * 1024 * 1024,
                          "open_files": prof["open_files"], "processes": prof["processes"]}}
        out_path, err_path = Path(scratch) / ".result", Path(scratch) / ".stderr"
        with open(out_path, "wb") as out, open(err_path, "wb") as err:
            kwargs = {"start_new_session": True} if os.name == "posix" else {}
            proc = subprocess.Popen([sys.executable, "-I", str(BOOT)], stdin=subprocess.PIPE, stdout=out,
                                    stderr=err, env=_env(scratch), cwd=scratch, **kwargs)
            if on_start is not None:
                on_start()
            try:
                proc.communicate(json.dumps(cfg).encode(), timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill(proc)
                return {"ok": False, "error_type": "timeout", "code": "timeout",
                        "error": f"operation exceeded {timeout:g}s", "reason": f"exceeded {timeout:g}s"}
        with open(out_path, "rb") as fh:
            raw = fh.read(MAX_RESULT + 1)
        result = None
        for line in reversed(raw.decode("utf-8", "replace").splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    result = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
        if len(raw) > MAX_RESULT:
            return {"ok": False, "error_type": "operation_failed", "code": "unsupported_output",
                    "error": "the result is larger than 8 MB", "reason": "the result is larger than 8 MB"}
        if not isinstance(result, dict):
            tail = err_path.read_bytes()[-600:].decode("utf-8", "replace") if err_path.exists() else ""
            why = "the process was stopped for exceeding its memory or CPU limits" if proc.returncode and \
                proc.returncode < 0 else f"the process exited with code {proc.returncode}"
            return {"ok": False, "error_type": "worker_crashed", "code": "sandbox_crashed",
                    "error": why, "reason": why, "stderr_tail": tail}
        return result
    finally:
        if owns and writable:
            _own([Path(p) for p in writable], 0)
        shutil.rmtree(scratch, ignore_errors=True)


def _kill(proc: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        pass
