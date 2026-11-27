"""Sandbox entry point: `python -I app/imports/sandbox_boot.py`, config as JSON on stdin.

Self-contained (stdlib only) and runs the lockdown BEFORE any untrusted code is imported:
  1. resource limits: address space, CPU seconds, processes, file size, open files, no core dumps
  2. drop root to a random unprivileged uid/gid with no supplementary groups
  3. PR_SET_DUMPABLE=0 (no /proc peeking or ptrace by other processes), PR_SET_NO_NEW_PRIVS=1
  4. seccomp filter (x86-64): sockets other than AF_UNIX, ptrace, process_vm_*, bpf, io_uring,
     namespaces/mount, keyctl, perf, userfaultfd are refused; foreign syscall ABIs kill the process.
     Only the "fetch" step (package managers downloading dependencies, with install scripts
     disabled, so no repo code runs) is started with network=True, which permits sockets.
Then the trusted runner is imported, the untrusted import paths are added, and the mode runs.
The environment arrives empty (no secrets); the parent enforces wall-clock timeouts.
The result is written as one JSON line to the original stdout; fd 1 is redirected to stderr so
library output can't corrupt it.
"""
import ctypes
import json
import os
import platform
import struct
import sys


def _seccomp_filter(network: bool = False) -> bytes:
    LD_W_ABS, JEQ_K, JGE_K, RET_K = 0x20, 0x15, 0x35, 0x06
    ALLOW, KILL = 0x7FFF0000, 0x80000000
    ERRNO = lambda e: 0x00050000 | e  # noqa: E731
    EPERM, EACCES = 1, 13
    AUDIT_ARCH_X86_64 = 0xC000003E
    SYS_SOCKET = 41
    deny = [101,  # ptrace
            310, 311,  # process_vm_readv / writev
            321,  # bpf
            425, 426, 427,  # io_uring_setup / enter / register
            272, 308,  # unshare, setns
            165, 166, 155, 167,  # mount, umount2, pivot_root, swapon
            250, 248, 249,  # keyctl, add_key, request_key
            298,  # perf_event_open
            323,  # userfaultfd
            246, 320,  # kexec_load, kexec_file_load
            175, 176, 313,  # init_module, delete_module, finit_module
            169,  # reboot
            ]
    prog = []  # (code, jt, jf, k) with symbolic jump targets resolved below
    prog.append(("ld", 4))
    prog.append(("jeq", AUDIT_ARCH_X86_64, "next", "kill"))
    prog.append(("ld", 0))
    prog.append(("jge", 0x40000000, "kill", "next"))  # x32 ABI
    for nr in deny:
        prog.append(("jeq", nr, "deny", "next"))
    if not network:  # dependency fetching (no repo code runs) is the only mode with network access
        prog.append(("jeq", SYS_SOCKET, "sock", "next"))
    prog.append(("ret", ALLOW))
    labels = {}
    prog.append(("label", "kill"))
    prog.append(("ret", KILL))
    prog.append(("label", "deny"))
    prog.append(("ret", ERRNO(EPERM)))
    prog.append(("label", "sock"))
    prog.append(("ld", 16))  # args[0] (domain), low 32 bits on little-endian
    prog.append(("jeq", 1, "next", "sockdeny"))  # AF_UNIX
    prog.append(("ret", ALLOW))
    prog.append(("label", "sockdeny"))
    prog.append(("ret", ERRNO(EACCES)))
    real = []
    for item in prog:
        if item[0] == "label":
            labels[item[1]] = len(real)
        else:
            real.append(item)
    out = b""
    for i, item in enumerate(real):
        if item[0] == "ld":
            out += struct.pack("HBBI", LD_W_ABS, 0, 0, item[1])
        elif item[0] == "ret":
            out += struct.pack("HBBI", RET_K, 0, 0, item[1])
        else:
            code = JEQ_K if item[0] == "jeq" else JGE_K
            jt = 0 if item[2] == "next" else labels[item[2]] - i - 1
            jf = 0 if item[3] == "next" else labels[item[3]] - i - 1
            assert 0 <= jt < 256 and 0 <= jf < 256
            out += struct.pack("HBBI", code, jt, jf, item[1])
    return out


def _lockdown(cfg: dict) -> None:
    import resource
    lim = cfg["limits"]
    for name, value in (("RLIMIT_CORE", 0), ("RLIMIT_AS", lim["memory_bytes"]), ("RLIMIT_CPU", lim["cpu_seconds"]),
                        ("RLIMIT_FSIZE", lim["file_bytes"]), ("RLIMIT_NOFILE", lim["open_files"]),
                        ("RLIMIT_NPROC", lim["processes"])):
        resource.setrlimit(getattr(resource, name), (value, value))
    uid = cfg["uid"]
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(uid)
        os.setuid(uid)
    if os.geteuid() == 0 or os.getuid() == 0:
        raise SystemExit("still root after dropping privileges")
    libc = ctypes.CDLL(None, use_errno=True)
    PR_SET_DUMPABLE, PR_SET_NO_NEW_PRIVS, PR_SET_SECCOMP, SECCOMP_MODE_FILTER = 4, 38, 22, 2
    if libc.prctl(PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_DUMPABLE")
    if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_NO_NEW_PRIVS")
    filt = _seccomp_filter(bool(cfg.get("network")))
    buf = ctypes.create_string_buffer(filt, len(filt))

    class SockFprog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]

    prog = SockFprog(len(filt) // 8, ctypes.cast(buf, ctypes.c_void_p))
    if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(prog), 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_SECCOMP")


def main() -> None:
    cfg = json.loads(sys.stdin.buffer.read())
    out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    os.dup2(2, 1)  # anything printed by libraries goes to stderr
    sys.stdout = sys.stderr
    strict = cfg.get("strict", True)
    if strict:
        why = None
        if sys.platform != "linux" or platform.machine() not in ("x86_64", "AMD64"):
            why = f"strict sandbox needs Linux x86-64 (this is {sys.platform}/{platform.machine()})"
        elif os.geteuid() != 0:
            why = "strict sandbox must start as root to drop to an unprivileged user"
        if why:
            out.write(json.dumps({"ok": False, "code": "sandbox_unavailable", "error_type": "operation_failed",
                                  "reason": why, "error": why}) + "\n")
            out.flush()
            return
        try:
            _lockdown(cfg)
        except BaseException as exc:  # noqa: BLE001
            msg = f"sandbox lockdown failed: {type(exc).__name__}: {exc}"
            out.write(json.dumps({"ok": False, "code": "sandbox_unavailable", "error_type": "operation_failed",
                                  "reason": msg, "error": msg}) + "\n")
            out.flush()
            return
    elif sys.platform == "linux":
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    sys.path.insert(0, cfg["app_dir"])  # trusted runner first, before any untrusted path exists
    from app.imports import runner
    for p in reversed(cfg.get("paths", [])):
        sys.path.insert(0, p)
    sys.dont_write_bytecode = True
    try:
        result = runner.MODES[cfg["mode"]](cfg["payload"])
    except BaseException as exc:  # noqa: BLE001
        result = {"ok": False, "code": "internal_error", "error_type": "operation_failed",
                  "reason": f"{type(exc).__name__}: {exc}"[:500], "error": f"{type(exc).__name__}: {exc}"[:500]}
    out.write("\n" + json.dumps(result, default=str) + "\n")
    out.flush()


if __name__ == "__main__":
    main()
