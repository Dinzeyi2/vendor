"""Entry point for a Python library worker: ``python -m app.worker_main <module>``.

Protocol (JSON lines over stdin/stdout):
  worker -> {"ready": true, "operations": [...]}        once its library has been imported
  api    -> {"id": n, "op": "...", "inputs": {...}}
  worker -> {"id": n, "ok": true, "output": ...}
         or {"id": n, "ok": false, "error_type": "invalid_input"|"operation_failed", "error": "..."}
  worker -> {"fatal": "..."}                             just before exiting on its own

The protocol uses a private duplicate of stdout; anything the libraries print goes to stderr.
"""
from __future__ import annotations

import importlib
import json
import os
import stat
import sys
import traceback


def _stdin_state() -> str:
    """Describe fd 0 for exit diagnostics (type and blocking mode)."""
    try:
        st = os.fstat(0)
        kind = "pipe" if stat.S_ISFIFO(st.st_mode) else "char-device" if stat.S_ISCHR(st.st_mode) else "other"
    except OSError as exc:
        return f"fd0 unavailable ({exc})"
    try:
        blocking = os.get_blocking(0)
    except OSError:
        blocking = "unknown"
    return f"fd0 type={kind} blocking={blocking}"


def main() -> None:
    module_name = sys.argv[1]
    proto = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)  # native-library output on fd 1 now lands on stderr
    sys.stdout = sys.stderr

    def send(msg: dict) -> None:
        proto.write(json.dumps(msg, allow_nan=False) + "\n")
        proto.flush()

    def fatal(reason: str) -> None:
        print(f"worker {module_name} exiting: {reason}", file=sys.stderr, flush=True)
        try:
            send({"fatal": reason})
        except Exception:
            pass

    try:
        from app.errors import OperationError
        from app.jsonutil import to_jsonable

        print(f"worker {module_name} importing; {_stdin_state()}", file=sys.stderr, flush=True)
        operations = importlib.import_module(module_name).OPERATIONS
        print(f"worker {module_name} ready; {_stdin_state()}", file=sys.stderr, flush=True)
        send({"ready": True, "operations": sorted(operations)})

        handled = 0
        for raw in sys.stdin.buffer:
            if not raw.strip():
                continue
            req = json.loads(raw)
            rid = req.get("id")
            try:
                output = to_jsonable(operations[req["op"]](req.get("inputs") or {}))
                send({"id": rid, "ok": True, "output": output})
            except OperationError as exc:
                send({"id": rid, "ok": False, "error_type": "invalid_input", "error": str(exc)})
            except Exception as exc:  # library failure: report it and keep serving
                traceback.print_exc()
                send({"id": rid, "ok": False, "error_type": "operation_failed",
                      "error": f"{type(exc).__name__}: {exc}"[:2000]})
            handled += 1
        fatal(f"stdin reached EOF after {handled} request(s); {_stdin_state()}")
    except BaseException as exc:
        traceback.print_exc()
        fatal(f"{type(exc).__name__}: {exc}"[:500])
        raise


if __name__ == "__main__":
    main()
