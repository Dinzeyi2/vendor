"""Worker profiles: the resources an import gets, chosen from what the repo actually needs.

Every import starts on `standard`. When a measurement crosses a standard limit (a repository over
25 MB, dependencies over 400 MB, a native or Node build, a project that declares heavy ML
frameworks), it moves to `large` instead of failing, up to `large`'s hard ceilings and the free
disk space. A repo configuration can also ask for a profile ("profile": "large").

The same rules apply to every repo: nothing here names a repository.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable

MB = 1024 * 1024
PROFILES: Dict[str, Dict[str, Any]] = {
    "standard": {"download_mb": 25, "extracted_mb": 100, "files": 5000, "deps_mb": 400,
                 "build_memory_mb": 6144, "build_timeout": 1500,
                 "call_memory_mb": 2048, "call_timeout": 30, "call_timeout_max": 120},
    "large": {"download_mb": 300, "extracted_mb": 1500, "files": 60000, "deps_mb": 6144,
              "build_memory_mb": 12288, "build_timeout": 3600,
              "call_memory_mb": 8192, "call_timeout": 120, "call_timeout_max": 900},
}
CEILING = PROFILES["large"]          # fetch and install limits never go above these
HEAVY_DEPENDENCIES = {"torch", "tensorflow", "jax", "jaxlib", "paddlepaddle", "mxnet", "onnxruntime", "opencv-python",
                      "opencv-python-headless", "scipy", "scikit-learn", "xgboost", "lightgbm", "pyarrow", "gdal"}
DISK_RESERVE_MB = 2048               # never fill the disk: keep this much free for the service itself


class ProfileError(Exception):
    pass


def get(name: str) -> Dict[str, Any]:
    return PROFILES.get(name) or PROFILES["standard"]


def choose(*, download_bytes: int = 0, extracted_bytes: int = 0, files: int = 0, deps_bytes: int = 0,
           runtime: str = "python", requirements: Iterable[str] = (), requested: str | None = None) -> str:
    """The smallest profile that fits the measurements (and the configuration's request)."""
    if requested in PROFILES:
        return requested
    std = PROFILES["standard"]
    names = {r.split(";")[0].split("[")[0].split("=")[0].split("<")[0].split(">")[0].split("~")[0].strip().lower()
             for r in requirements if isinstance(r, str)}
    if (download_bytes > std["download_mb"] * MB or extracted_bytes > std["extracted_mb"] * MB or files > std["files"]
            or deps_bytes > std["deps_mb"] * MB or names & HEAVY_DEPENDENCIES):
        return "large"
    return "standard"


def free_mb(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    return int(shutil.disk_usage(path).free // MB)


def require_disk(path: Path, need_mb: int, what: str) -> None:
    free = free_mb(path)
    if free - need_mb < DISK_RESERVE_MB:
        from app.imports.github import ImportFailure
        raise ImportFailure("insufficient_resources", f"{what} needs about {need_mb} MB of disk, but only {free} MB is "
                                                       f"free on the build workers (keeping {DISK_RESERVE_MB} MB in reserve).",
                            free_mb=free, need_mb=need_mb)


def size_of(path: Path) -> int:
    total = 0
    for dirpath, _, filenames in os.walk(path):
        for f in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, f)).st_size
            except OSError:
                pass
    return total
