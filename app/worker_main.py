"""Entry point for a Python library worker: ``python -m app.worker_main <module>``.

Protocol (JSON lines over private copies of stdin/stdout):
  worker -> {"ready": true, "operations": [...], "notes": [...]}   once its library is imported
  api    -> {"id": n, "op": "...", "inputs": {...}}
  worker -> {"id": n, "ok": true, "output": ...}
         or {"id": n, "ok": false, "error_type": "invalid_input"|"operation_failed", "error": "..."}
  worker -> {"note": "..."}                                        diagnostic, any time
  worker -> {"fatal": "..."}                                       just before exiting on its own

Before the library is imported, the protocol pipes are moved to private file descriptors:
fd 1 is pointed at stderr and fd 0 at /dev/null. Libraries with their own I/O runtime
(Julia's libuv in PySR) take over fds 0-2 when they start -- libuv puts them in
non-blocking mode -- and must not be able to touch the protocol pipes.
"""
from __future__ import annotations

import importlib
import json
import os
import stat
import sys
import traceback
from typing import Callable, Iterator


def _fd_state(fd: int) -> str:
    """Describe a file descriptor for diagnostics: its type and blocking mode."""
    try:
        st = os.fstat(fd)
    except OSError as exc:
        return f"unavailable ({exc.strerror})"
    kind = ("pipe" if stat.S_ISFIFO(st.st_mode) else "char-device" if stat.S_ISCHR(st.st_mode)
            else "file" if stat.S_ISREG(st.st_mode) else "other")
    try:
        mode = "blocking" if os.get_blocking(fd) else "NON-BLOCKING"
    except OSError:
        mode = "mode-unknown"
    return f"{kind}/{mode}"


def _read_lines(fd: int, note: Callable[[str], None]) -> Iterator[bytes]:
    """Yield newline-terminated requests from fd, staying correct if fd becomes non-blocking."""
    buf = b""
    warned = False
    while True:
        try:
            chunk = os.read(fd, 1 << 16)
        except BlockingIOError:
            if not warned:
                note(f"protocol input was switched to non-blocking ({_fd_state(fd)}); restored blocking mode")
                warned = True
            os.set_blocking(fd, True)
            continue
        except InterruptedError:
            continue
        if not chunk:  # real EOF: the API closed the pipe
            if buf.strip():
                yield buf
            return
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line


def main() -> None:
    module_name = sys.argv[1]

    # Private protocol channels, then hand the standard fds to the libraries.
    proto_in = os.dup(0)
    proto = os.fdopen(os.dup(1), "w", encoding="utf-8", buffering=1)
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    os.dup2(2, 1)  # native-library output on fd 1 now lands on stderr
    sys.stdout = sys.stderr
    sys.stdin = open(os.devnull, "r")

    def log(msg: str) -> None:
        print(f"worker {module_name}: {msg}", file=sys.stderr, flush=True)

    def send(msg: dict) -> None:
        proto.write(json.dumps(msg, allow_nan=False) + "\n")
        proto.flush()

    def note(msg: str) -> None:
        log(msg)
        try:
            send({"note": msg})
        except Exception:
            pass

    def fatal(reason: str) -> None:
        log(f"exiting: {reason}")
        try:
            send({"fatal": reason})
        except Exception:
            pass

    try:
        from app.errors import OperationError
        from app.jsonutil import to_jsonable

        before = {fd: _fd_state(fd) for fd in (0, 1, 2)}
        operations = importlib.import_module(module_name).OPERATIONS
        after = {fd: _fd_state(fd) for fd in (0, 1, 2)}
        notes = [f"importing {module_name} changed fd {fd} from {before[fd]} to {after[fd]}"
                 for fd in (0, 1, 2) if before[fd] != after[fd]]
        for n in notes:
            log(n)
        log(f"ready; protocol input {_fd_state(proto_in)}")
        send({"ready": True, "operations": sorted(operations), "notes": notes})

        handled = 0
        for raw in _read_lines(proto_in, note):
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
        fatal(f"protocol input reached EOF after {handled} request(s)")
    except BaseException as exc:
        traceback.print_exc()
        fatal(f"{type(exc).__name__}: {exc}"[:500])
        raise


if __name__ == "__main__":
    main()
