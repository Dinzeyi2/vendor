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


def npm_cli() -> str | None:
    """npm's JavaScript entry point next to node_bin(), run as `node npm-cli.js` (no shell scripts)."""
    node = Path(node_bin()).resolve() if Path(node_bin()).exists() else Path(shutil.which(node_bin()) or node_bin())
    for rel in ("node_modules/npm/bin/npm-cli.js", "../lib/node_modules/npm/bin/npm-cli.js",
                "../share/nodejs/npm/bin/npm-cli.js", "/usr/share/nodejs/npm/bin/npm-cli.js"):
        p = (node.parent / rel) if not rel.startswith("/") else Path(rel)
        if p.is_file():
            return str(p.resolve())
    return None


def node_headers() -> str | None:
    """Node's install prefix when it ships C headers (include/node), so native addons build offline."""
    node = Path(node_bin()).resolve() if Path(node_bin()).exists() else None
    for prefix in ([node.parent.parent, node.parent] if node else []):
        if (prefix / "include" / "node" / "node_api.h").is_file():
            return str(prefix)
    return None


def tool(name: str) -> str | None:
    """A build tool on the server (cmake, make, cc, c++, git...), or None if it isn't installed."""
    return shutil.which(name)
