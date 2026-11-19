"""Entry point for a Python library worker: ``python -m app.worker_main <module>``.

Protocol (JSON lines over stdin/stdout):
  worker -> {"ready": true, "operations": [...]}        once its library has been imported
  api    -> {"id": n, "op": "...", "inputs": {...}}
  worker -> {"id": n, "ok": true, "output": ...}
         or {"id": n, "ok": false, "error_type": "invalid_input"|"operation_failed", "error": "..."}

The protocol uses a private duplicate of stdout; anything the libraries print goes to stderr.
"""
from __future__ import annotations

import importlib
import json
import os
import sys
import traceback


def main() -> None:
    module_name = sys.argv[1]
    proto = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    os.dup2(2, 1)  # native-library output on fd 1 now lands on stderr
    sys.stdout = sys.stderr

    from app.errors import OperationError
    from app.jsonutil import to_jsonable

    def send(msg: dict) -> None:
        proto.write(json.dumps(msg, allow_nan=False) + "\n")
        proto.flush()

    operations = importlib.import_module(module_name).OPERATIONS
    send({"ready": True, "operations": sorted(operations)})

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


if __name__ == "__main__":
    main()
