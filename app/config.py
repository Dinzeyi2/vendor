"""Runtime locations. Environment variables win; otherwise use project-local .runtimes/."""
from __future__ import annotations

import glob
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNTIMES = ROOT / ".runtimes"


def _first(pattern: str) -> str | None:
    matches = sorted(glob.glob(str(RUNTIMES / pattern)))
    return matches[-1] if matches else None


def java_home() -> str | None:
    """JDK 17+ for Timefold (JPype starts the JVM inside the timefold worker)."""
    return os.environ.get("JAVA_HOME") or _first("jdk-*")


def node_bin() -> str:
    """Node.js 18+ for the Rapier worker."""
    if os.environ.get("NODE_BIN"):
        return os.environ["NODE_BIN"]
    exe = "node.exe" if sys.platform == "win32" else "bin/node"
    return _first(f"node-*/{exe}") or shutil.which("node") or "node"
