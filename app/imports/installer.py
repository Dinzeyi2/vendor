"""Install an imported repo's dependencies into its own folder. Trusted code.

Wheels only (`--only-binary=:all:`): installing a wheel copies files and never runs package code,
so nothing untrusted executes here even though this step needs the network (pip talks to PyPI
only). Requirements the base environment already satisfies are skipped; the installed set is
recorded as exact pins so the import can be rebuilt identically after a redeploy.
The folder goes on the sandbox's sys.path (not as a site dir, so .pth files are never processed).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from importlib import metadata
from pathlib import Path
from typing import List

from app.imports.github import ImportFailure

MAX_SITE_BYTES = 400 * 1024 * 1024
INDEX = "https://pypi.org/simple"


def _satisfied(req: str) -> bool:
    try:
        from packaging.requirements import Requirement
    except ImportError:  # pip's vendored copy
        from pip._vendor.packaging.requirements import Requirement
    try:
        r = Requirement(req)
    except Exception:  # noqa: BLE001
        return False
    if r.marker is not None and not r.marker.evaluate():
        return True  # not needed on this platform
    try:
        version = metadata.version(r.name)
    except metadata.PackageNotFoundError:
        return False
    return not r.specifier or r.specifier.contains(version, prereleases=True)


def _size(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _pip(args: List[str], timeout: float) -> subprocess.CompletedProcess:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": tempfile.gettempdir(),
           "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1", "LANG": "C.UTF-8"}
    if sys.platform == "win32":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
    try:
        return subprocess.run([sys.executable, "-m", "pip", *args], capture_output=True, text=True,
                              timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise ImportFailure("unsupported_dependency", "Installing the dependencies took longer than "
                                                      f"{timeout:g}s.") from None


def _explain(stderr: str) -> str:
    m = re.search(r"No matching distribution found for ([^\s]+)", stderr)
    if m:
        return (f"Dependency '{m.group(1)}' has no prebuilt package (wheel) for Linux / Python "
                f"{sys.version_info.major}.{sys.version_info.minor}; Cognex doesn't build packages from source.")
    m = re.search(r"ResolutionImpossible|conflicting dependencies", stderr)
    if m:
        return "The dependencies conflict with each other and can't be installed together."
    tail = " ".join(stderr.strip().splitlines()[-3:])[:300]
    return f"The dependencies couldn't be installed: {tail}"


def install(requirements: List[str], site: Path, timeout: float = 300) -> List[str]:
    """Install what's missing into `site`; returns exact pins (name==version) of what was installed."""
    site.mkdir(parents=True, exist_ok=True)
    needed = [r for r in requirements if not _satisfied(r)]
    if not needed:
        return []
    with tempfile.TemporaryDirectory() as tmp:
        report = Path(tmp) / "report.json"
        proc = _pip(["install", "--target", str(site), "--only-binary=:all:", "--index-url", INDEX,
                     "--no-warn-script-location", "--report", str(report), *needed], timeout)
        if proc.returncode != 0:
            raise ImportFailure("unsupported_dependency", _explain(proc.stderr), requirements=needed)
        pins = []
        try:
            for item in json.loads(report.read_text(encoding="utf-8")).get("install", []):
                md = item.get("metadata", {})
                if md.get("name") and md.get("version"):
                    pins.append(f"{md['name']}=={md['version']}")
        except (OSError, json.JSONDecodeError):
            pass
    if _size(site) > MAX_SITE_BYTES:
        raise ImportFailure("unsupported_dependency",
                            f"The dependencies are larger than {MAX_SITE_BYTES // (1024 * 1024)} MB.")
    return sorted(pins)


def reinstall(pins: List[str], site: Path, timeout: float = 300) -> None:
    """Rebuild a ready import's dependency folder from its recorded exact pins."""
    site.mkdir(parents=True, exist_ok=True)
    if not pins:
        return
    proc = _pip(["install", "--target", str(site), "--only-binary=:all:", "--no-deps", "--index-url", INDEX,
                 "--no-warn-script-location", *pins], timeout)
    if proc.returncode != 0:
        raise ImportFailure("unsupported_dependency", _explain(proc.stderr))
