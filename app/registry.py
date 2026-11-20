"""Operation registry: which worker runs each operation, and its timeouts.

RapidFuzz runs inline (microseconds). Every other library gets its own persistent worker
process, so heavy runtimes (PyTorch, PaddlePaddle, the JVM, Julia, Node/WASM) are isolated:
a hang or crash in one cannot affect the API process or the other libraries.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

from app import catalog
from app.config import ROOT, java_home, node_bin
from app.ops import rapidfuzz_ops
from app.workers import WorkerProcess


@dataclass(frozen=True)
class OpSpec:
    library: str
    default_timeout: float  # seconds, used when the task gives no timeout_s
    max_timeout: float      # upper bound for a task's timeout_s
    inline: Optional[Callable] = None
    worker: Optional[str] = None
    internal: Optional[str] = None  # name inside the worker; defaults to the public name


def _py(module: str, env: Optional[Dict[str, str]] = None) -> Dict:
    return {"cmd": [sys.executable, "-m", "app.worker_main", module], "env": env or {}}


def _java_env() -> Dict[str, str]:
    jh = java_home()
    if not jh:
        return {}
    # JPype loads jvm.dll in-process; the JVM's own native libraries (e.g. instrument.dll on
    # Windows) are resolved through PATH, so the JDK's bin directory must be on it.
    return {"JAVA_HOME": jh, "PATH": os.path.join(jh, "bin") + os.pathsep + os.environ.get("PATH", "")}


def _worker_defs() -> Dict[str, Dict]:
    return {
        "hnswlib": _py("app.ops.hnswlib_ops"),
        "xgboost": _py("app.ops.xgboost_ops"),
        "scs": _py("app.ops.scs_ops"),
        "dowhy": _py("app.ops.dowhy_ops"),
        "kornia": _py("app.ops.kornia_ops"),
        "paddleocr": _py("app.ops.paddleocr_ops", {"PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True"}),
        "pysr": _py("app.ops.pysr_ops", {"PYTHON_JULIACALL_HANDLE_SIGNALS": "yes"}),
        "timefold": _py("app.ops.timefold_ops", _java_env()),
        "rapier": {"cmd": [node_bin(), str(ROOT / "workers" / "rapier" / "worker.mjs")], "env": {}},
    }


# Keyed by public Cognex name (app/catalog.py). `internal` is the name the worker knows;
# the original library-based names are accepted as aliases via lookup().
OPERATIONS: Dict[str, OpSpec] = {
    "text.similarity":               OpSpec("RapidFuzz", 5, 10, inline=rapidfuzz_ops.compare,
                                            internal="rapidfuzz.compare"),
    "vectors.nearest_neighbors":     OpSpec("hnswlib", 30, 120, worker="hnswlib", internal="hnswlib.knn_query"),
    "tabular.train_and_predict":     OpSpec("XGBoost", 60, 300, worker="xgboost", internal="xgboost.train_predict"),
    "optimization.solve_convex":     OpSpec("SCS", 30, 300, worker="scs", internal="scs.solve"),
    "causal.estimate_effect":        OpSpec("DoWhy", 60, 300, worker="dowhy", internal="dowhy.estimate_effect"),
    "image.detect_edges":            OpSpec("Kornia", 60, 120, worker="kornia", internal="kornia.canny"),
    "image.extract_text":            OpSpec("PaddleOCR", 120, 300, worker="paddleocr", internal="paddleocr.ocr"),
    "math.discover_equation":        OpSpec("PySR", 300, 900, worker="pysr", internal="pysr.symbolic_regression"),
    "scheduling.school_timetable":   OpSpec("Timefold Solver", 60, 180, worker="timefold",
                                            internal="timefold.school_timetabling"),
    "physics.simulate_rigid_bodies": OpSpec("Rapier", 30, 120, worker="rapier", internal="rapier.simulate"),
}


def lookup(name: str) -> Optional[Tuple[str, OpSpec]]:
    """(public name, spec) for a public name or a compatibility alias; None if unknown."""
    public = catalog.canonical(name)
    spec = OPERATIONS.get(public)
    return (public, spec) if spec is not None else None


WORKERS: Dict[str, WorkerProcess] = {
    name: WorkerProcess(name, d["cmd"], d["env"]) for name, d in _worker_defs().items()
}


def warm_list() -> list[str]:
    """WARM_WORKERS=all | comma list of worker names to start at boot."""
    raw = os.environ.get("WARM_WORKERS", "").strip()
    if raw == "all":
        return list(WORKERS)
    return [w.strip() for w in raw.split(",") if w.strip() in WORKERS]
