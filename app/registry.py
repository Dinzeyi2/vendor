"""Operation registry: which worker runs each operation, and its timeouts.

RapidFuzz runs inline (microseconds). Every other library gets its own persistent worker
process, so heavy runtimes (PyTorch, PaddlePaddle, the JVM, Julia, Node/WASM) are isolated:
a hang or crash in one cannot affect the API process or the other libraries.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Callable, Dict, Optional

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


OPERATIONS: Dict[str, OpSpec] = {
    "rapidfuzz.compare":           OpSpec("RapidFuzz", 5, 10, inline=rapidfuzz_ops.compare),
    "hnswlib.knn_query":           OpSpec("hnswlib", 30, 120, worker="hnswlib"),
    "xgboost.train_predict":       OpSpec("XGBoost", 60, 300, worker="xgboost"),
    "scs.solve":                   OpSpec("SCS", 30, 300, worker="scs"),
    "dowhy.estimate_effect":       OpSpec("DoWhy", 60, 300, worker="dowhy"),
    "kornia.canny":                OpSpec("Kornia", 60, 120, worker="kornia"),
    "paddleocr.ocr":               OpSpec("PaddleOCR", 120, 300, worker="paddleocr"),
    "pysr.symbolic_regression":    OpSpec("PySR", 300, 900, worker="pysr"),
    "timefold.school_timetabling": OpSpec("Timefold Solver", 60, 180, worker="timefold"),
    "rapier.simulate":             OpSpec("Rapier", 30, 120, worker="rapier"),
}

WORKERS: Dict[str, WorkerProcess] = {
    name: WorkerProcess(name, d["cmd"], d["env"]) for name, d in _worker_defs().items()
}


def warm_list() -> list[str]:
    """WARM_WORKERS=all | comma list of worker names to start at boot."""
    raw = os.environ.get("WARM_WORKERS", "").strip()
    if raw == "all":
        return list(WORKERS)
    return [w.strip() for w in raw.split(",") if w.strip() in WORKERS]
