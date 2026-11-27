"""Dependencies published only as source (an sdist, no wheel): build them into wheels, offline, in the
sandbox, so the trusted installer still only ever installs wheels.

  1. pin      the newest release that satisfies the requirement and has an sdist (PyPI JSON API)
  2. fetch    that exact file over HTTPS and check it against PyPI's SHA-256 (trusted, no code runs)
  3. tools    read its build requirements from pyproject.toml as data (default: setuptools) and install
              them as wheels into a build environment
  4. build    `pip wheel --no-build-isolation --no-index` in the sandbox: no network, no secrets,
              unprivileged; the package's setup code runs only there
The wheel lands in a local wheelhouse the installer then uses like an index.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import sys
import tarfile
import tempfile
import tomllib
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

from app.imports.github import ImportFailure

PYPI_JSON = "https://pypi.org/pypi/{name}/json"
MAX_SDIST = 100 * 1024 * 1024
BUILD_TIMEOUT = 900
DEFAULT_BUILD_REQUIRES = ["setuptools>=70.1"]


def _requirement(req: str):
    try:
        from packaging.requirements import Requirement
    except ImportError:  # pip's vendored copy
        from pip._vendor.packaging.requirements import Requirement
    return Requirement(req)


def _version(v: str):
    try:
        from packaging.version import Version
    except ImportError:
        from pip._vendor.packaging.version import Version
    return Version(v)


def pin(req: str) -> Tuple[str, str, Dict[str, Any]]:
    """(name, version, sdist file record) for the newest release matching `req` that has an sdist."""
    r = _requirement(req)
    try:
        with urllib.request.urlopen(urllib.request.Request(PYPI_JSON.format(name=r.name),
                                                           headers={"User-Agent": "Cognex-Importer/1.0"}),
                                    timeout=30) as resp:  # noqa: S310 (fixed https host)
            data = json.loads(resp.read(20 * 1024 * 1024))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        raise ImportFailure("unsupported_dependency", f"PyPI has no package '{r.name}' (or couldn't be reached).",
                            requirement=req) from None
    candidates = []
    for v, files in (data.get("releases") or {}).items():
        try:
            ver = _version(v)
        except Exception:  # noqa: BLE001 - unparseable legacy versions
            continue
        if ver.is_prerelease and not r.specifier.prereleases:
            continue
        if r.specifier and not r.specifier.contains(v, prereleases=True):
            continue
        sdist = next((f for f in files if f.get("packagetype") == "sdist" and not f.get("yanked")), None)
        if sdist:
            candidates.append((ver, v, sdist))
    if not candidates:
        raise ImportFailure("unsupported_dependency", f"'{req}' has no wheel and no source release to build.",
                            requirement=req)
    _, version, record = max(candidates, key=lambda c: c[0])
    return r.name, version, record


def _fetch(record: Dict[str, Any], dest: Path) -> Path:
    url, want = record["url"], (record.get("digests") or {}).get("sha256")
    if not url.startswith("https://files.pythonhosted.org/") or not want:
        raise ImportFailure("unsupported_dependency", f"Unexpected source location {url[:120]}.")
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Cognex-Importer/1.0"}),
                                timeout=120) as resp:  # noqa: S310 (PyPI's file host only)
        blob = resp.read(MAX_SDIST + 1)
    if len(blob) > MAX_SDIST:
        raise ImportFailure("insufficient_resources", f"The source release {record['filename']} is larger than "
                                                       f"{MAX_SDIST // (1024 * 1024)} MB.")
    if hashlib.sha256(blob).hexdigest() != want:
        raise ImportFailure("unsupported_dependency", f"{record['filename']} doesn't match PyPI's SHA-256.")
    path = dest / record["filename"]
    path.write_bytes(blob)
    return path


def _extract(archive: Path, dest: Path) -> Path:
    """Safe extraction (no absolute paths, no '..', regular files and directories only)."""
    dest.mkdir(parents=True, exist_ok=True)
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                p = Path(info.filename)
                if p.is_absolute() or ".." in p.parts:
                    continue
                target = dest / p
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(info) as src, open(target, "wb") as fh:
                    shutil.copyfileobj(src, fh)
    else:
        with tarfile.open(archive) as t:
            t.extractall(dest, filter="data")  # rejects links outside, absolute paths, devices
    tops = [p for p in dest.iterdir()]
    return tops[0] if len(tops) == 1 and tops[0].is_dir() else dest


def build_requires(project: Path) -> List[str]:
    """[build-system] requires from pyproject.toml, read as data; setuptools when there is none."""
    py = project / "pyproject.toml"
    if py.is_file():
        try:
            req = (tomllib.loads(py.read_text(encoding="utf-8", errors="replace")).get("build-system") or {}).get("requires")
            if isinstance(req, list) and all(isinstance(x, str) for x in req):
                return req or DEFAULT_BUILD_REQUIRES
        except tomllib.TOMLDecodeError:
            pass
    return DEFAULT_BUILD_REQUIRES


def build_wheel(req: str, wheelhouse: Path, workdir: Path) -> Path:
    """Build the pinned source release of `req` into `wheelhouse`; returns the wheel."""
    from app.imports import installer, sandbox
    from app.imports.jobs import imports_dir
    name, version, record = pin(req)
    wheelhouse.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="sdist-", dir=str(workdir)))
    try:
        project = _extract(_fetch(record, tmp), tmp / "src")
        buildenv = tmp / "buildenv"
        installer.install(build_requires(project), buildenv)   # wheels only (skips what the server has)
        out = tmp / "out"
        out.mkdir()
        res = sandbox.run("exec", {"steps": [{"argv": [sys.executable, "-m", "pip", "wheel", "--no-deps",
                                                       "--no-build-isolation", "--no-index", "--wheel-dir", str(out),
                                                       str(project)], "cwd": str(project)}],
                                   "env": {"PYTHONPATH": str(buildenv), "PYTHONNOUSERSITE": "1", "PIP_NO_INDEX": "1",
                                           "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1",
                                           "SETUPTOOLS_SCM_PRETEND_VERSION": version},
                                   "timeout": BUILD_TIMEOUT},
                          paths=[str(buildenv)], timeout=BUILD_TIMEOUT + 30, scratch_root=imports_dir() / "tmp",
                          profile="build", writable=[project, out, buildenv])
        wheels = sorted(out.glob("*.whl"))
        if not res.get("ok") or not wheels:
            tail = " ".join((res.get("log_tail") or res.get("reason") or "").split())[-500:]
            raise ImportFailure("unsupported_dependency", f"'{name}=={version}' has no wheel, and building its source "
                                                          f"release failed: {tail}", requirement=req)
        target = wheelhouse / wheels[0].name
        shutil.copyfile(wheels[0], target)
        return target
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
