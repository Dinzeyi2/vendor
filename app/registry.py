"""Operation registry, built from the capability manifests in app/capabilities/.

Text matching runs inline (microseconds). Every other library gets its own persistent worker
process, so heavy runtimes (PyTorch, PaddlePaddle, the JVM, Julia, Node/WASM) are isolated:
a hang or crash in one cannot affect the API process or the other libraries.

Adding a capability = adding a manifest (+ its adapter). Nothing in this file, the router or
the API changes; see .claude/skills/add-github-repo/SKILL.md.
"""
from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

from app import catalog, manifests
from app.config import ROOT, java_home, node_bin
from app.manifests import Capability
from app.workers import WorkerProcess


@dataclass(frozen=True)
class OpSpec:
    library: str
    default_timeout: float  # seconds, used when the task gives no timeout_s
    max_timeout: float      # upper bound for a task's timeout_s
    inline: Optional[Callable] = None
    worker: Optional[str] = None
    internal: Optional[str] = None  # name inside the worker; defaults to the public name


def _java_env() -> Dict[str, str]:
    jh = java_home()
    if not jh:
        return {}
    # JPype loads jvm.dll in-process; the JVM's own native libraries (e.g. instrument.dll on
    # Windows) are resolved through PATH, so the JDK's bin directory must be on it.
    return {"JAVA_HOME": jh, "PATH": os.path.join(jh, "bin") + os.pathsep + os.environ.get("PATH", "")}


def _worker_cmd(rt: Dict) -> Tuple[List[str], Dict[str, str]]:
    env = dict(rt.get("env", {}))
    if rt.get("env_profile") == "java":
        env.update(_java_env())
    if rt["kind"] == "node":
        return [node_bin(), str(ROOT / rt["script"])], env
    return [sys.executable, "-m", "app.worker_main", rt["module"]], env


def _spec(cap: Capability) -> OpSpec:
    rt = cap.runtime
    if rt["kind"] == "inline":
        fn = importlib.import_module(rt["module"]).OPERATIONS[rt["internal"]]
        return OpSpec(cap.built_on.project, cap.default_timeout, cap.max_timeout, inline=fn, internal=rt["internal"])
    return OpSpec(cap.built_on.project, cap.default_timeout, cap.max_timeout, worker=rt["worker"], internal=rt["internal"])


CAPABILITIES: Dict[str, Capability] = {}   # public name -> manifest
OPERATIONS: Dict[str, OpSpec] = {}          # public name -> how to run it
WORKERS: Dict[str, WorkerProcess] = {}


def register(cap: Capability) -> None:
    """Add (or replace) one capability: it is immediately runnable, listed and routable."""
    taken = {n: c.name for c in CAPABILITIES.values() if c.name != cap.name for n in (c.name, *c.aliases)}
    clash = [n for n in (cap.name, *cap.aliases) if n in taken]
    if clash:
        raise manifests.ManifestError(f"{cap.name}: names already registered: {clash}")
    rt = cap.runtime
    if rt["kind"] != "inline" and rt["worker"] not in WORKERS:
        cmd, env = _worker_cmd(rt)
        WORKERS[rt["worker"]] = WorkerProcess(rt["worker"], cmd, env)
    OPERATIONS[cap.name] = _spec(cap)
    CAPABILITIES[cap.name] = cap
    catalog.add(cap)


def unregister(name: str) -> None:
    cap = CAPABILITIES.pop(name, None)
    OPERATIONS.pop(name, None)
    if cap:
        catalog.remove(cap)


def load(directory=manifests.CAPABILITY_DIR) -> List[Capability]:
    caps = manifests.load_dir(directory)
    manifests.check_unique(caps + [c for c in CAPABILITIES.values() if c.name not in {x.name for x in caps}])
    for cap in caps:
        register(cap)
    return caps


load()
for _extra in filter(None, os.environ.get("COGNEX_EXTRA_CAPABILITIES", "").split(os.pathsep)):
    load(_extra)  # optional extra manifest directories (staging experiments)


def lookup(name: str) -> Optional[Tuple[str, OpSpec]]:
    """(public name, spec) for a public name or a compatibility alias; None if unknown."""
    public = catalog.canonical(name)
    spec = OPERATIONS.get(public)
    return (public, spec) if spec is not None else None


def warm_list() -> list[str]:
    """WARM_WORKERS=all | comma list of worker names to start at boot."""
    raw = os.environ.get("WARM_WORKERS", "").strip()
    if raw == "all":
        return list(WORKERS)
    return [w.strip() for w in raw.split(",") if w.strip() in WORKERS]
