"""Runtimes: how each kind of project is fetched, built, discovered and called. Trusted code that
drives untrusted work through the sandbox:

  fetch    package managers download the locked dependencies with install scripts disabled, so no
           repo code runs; network allowed, but as a random unprivileged uid with no secrets
  build    compilers, PEP 517 backends, npm scripts, CMake/Make: sandbox, no network
  discover find the operation and a real sample call: sandbox, no network
  call     production execution: sandbox, no network, per call

Builds are cached by build key (commit + plan + lockfile + toolchain), so repeated imports of the
same repo, by any customer, reuse them. A new repo is detection plus, when needed, a repo
configuration (app/imports/plan.py); code here changes only for a genuinely new runtime.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import mimetypes
import os
import platform
import re
import shutil
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.config import ROOT, node_bin, node_headers, npm_cli, tool
from app.imports import installer, packaging, profiles, sandbox
from app.imports.github import ImportFailure
from app.imports.plan import NeedsConfig, NeedsRuntime

BUILD_TIMEOUT = int(os.environ.get("COGNEX_BUILD_TIMEOUT", "1500"))
FETCH_TIMEOUT = 600
DISCOVER_TIMEOUT = 240
JOBS = str(max(1, min(4, os.cpu_count() or 1)))
MAX_ASSETS, MAX_ASSET_BYTES = 500, 50 * 1024 * 1024
_locks: Dict[str, threading.Lock] = {}


# -- build cache --------------------------------------------------------------------------------------
def _toolchain() -> Dict[str, str]:
    return {"python": platform.python_version(), "platform": f"{sys.platform}-{platform.machine()}",
            "node": os.path.basename(os.path.dirname(os.path.dirname(node_bin()))) or node_bin()}


def build_key(plan: Dict[str, Any], commit: str, src: Path) -> str:
    """Same commit + same build-relevant plan + same lockfile + same toolchain = same build."""
    base = src / plan["root"] if plan.get("root") else src
    locks = {}
    for name in ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "uv.lock",
                 "poetry.lock", "requirements.txt", "pdm.lock"):
        p = base / name
        if p.is_file():
            locks[name] = hashlib.sha256(p.read_bytes()).hexdigest()
    relevant = {k: plan.get(k) for k in ("runtime", "root", "package", "version", "extra_requirements", "build",
                                         "executables", "assets", "service")}
    relevant["service_config"] = (plan.get("config") or {}).get("service")
    raw = json.dumps({"commit": commit, "plan": relevant, "locks": locks, "toolchain": _toolchain(), "v": 1},
                     sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def builds_dir() -> Path:
    """Shared build cache (COGNEX_BUILDS_DIR, else <imports dir>/builds)."""
    if os.environ.get("COGNEX_BUILDS_DIR"):
        return Path(os.environ["COGNEX_BUILDS_DIR"])
    from app.imports.jobs import imports_dir
    return imports_dir() / "builds"


def ensure_build(key: str, make: Callable[[Path], Dict[str, Any]]) -> Tuple[Path, Dict[str, Any], bool]:
    """(build dir, build info, reused?) — builds once per key; later imports and calls reuse it."""
    final = builds_dir() / key
    lock = _locks.setdefault(key, threading.Lock())
    with lock:
        if (final / "build.json").is_file():
            return final, json.loads((final / "build.json").read_text()), True
        tmp = builds_dir() / f".{key}.{os.getpid()}.{int(time.time())}"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        try:
            info = make(tmp)
            # paths were recorded inside the temporary folder; point them at the final one
            info = json.loads(json.dumps(info).replace(json.dumps(str(tmp))[1:-1], json.dumps(str(final))[1:-1]))
            (tmp / "build.json").write_text(json.dumps(info))
            shutil.rmtree(final, ignore_errors=True)
            os.replace(tmp, final)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    return final, info, False


# -- sandbox helpers ----------------------------------------------------------------------------------
def _scratch() -> Path:
    from app.imports.jobs import imports_dir
    return imports_dir() / "tmp"


def _steps(steps: List[dict], *, network: bool, writable: List[Path], paths: List[str] = (), timeout: int,
           env: Optional[dict] = None, what: str = "build", plan: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    prof = profiles.get((plan or {}).get("profile", "standard"))
    timeout = max(timeout, prof["build_timeout"]) if not network else timeout
    res = sandbox.run("exec", {"steps": steps, "env": env or {}, "timeout": timeout}, paths=list(paths),
                      timeout=timeout + 30, scratch_root=_scratch(), profile="fetch" if network else "build",
                      network=network, writable=writable,
                      memory_mb=None if network else prof["build_memory_mb"])
    if res.get("ok"):
        return res
    if res.get("code") in ("sandbox_unavailable", "timeout", "sandbox_crashed"):
        raise ImportFailure("build_failed" if res.get("code") != "sandbox_unavailable" else "sandbox_unavailable",
                            f"The {what} step {('timed out after ' + str(timeout) + 's') if res.get('code') == 'timeout' else 'failed'}"
                            f": {res.get('reason', '')}"[:900])
    raise _explain_build(res, what)


def _explain_build(res: Dict[str, Any], what: str) -> ImportFailure:
    log = res.get("log_tail") or ""
    cmd = " ".join(res.get("failed") or [])[:200]
    low = log.lower()
    tail = "\n".join(log.strip().splitlines()[-12:])[-1500:]
    if re.search(r"could not resolve host|temporary failure in name resolution|network is unreachable|"
                 r"getaddrinfo|eai_again|failed to connect|connection refused|enotfound|permission denied \(13\)"
                 r".*socket|operation not permitted.*connect", low) and what != "fetch":
        return NeedsConfig(f"The {what} step tried to download something (`{cmd}`); builds run without network "
                           f"access, so everything must come from the lockfile or vendored sources.",
                           [{"field": "build.commands", "why": "build steps that don't download (vendor the files, or "
                                                               "pin them as dependencies)"}],
                           {"build": {"commands": [["..."]]}}, log_tail=tail)
    for marker, need in (("cargo", "a Rust toolchain (cargo/rustc)"), ("rustc", "a Rust toolchain (cargo/rustc)"),
                         ("go: command not found", "a Go toolchain"), ("javac", "a Java JDK"),
                         ("gfortran", "a Fortran compiler"), ("nvcc", "the CUDA toolkit")):
        if marker in low and ("not found" in low or "no such file" in low or "could not find" in low):
            return NeedsRuntime("toolchain", f"Building this project needs {need}, which the build workers "
                                             f"don't have.", [{"field": "runtime", "why": need}], log_tail=tail)
    m = re.search(r"(?:fatal error: |error: )([\w/.+-]+\.h): no such file", low)
    if m:
        return NeedsConfig(f"The build needs the C/C++ header {m.group(1)} from a system library that isn't "
                           f"installed on the build workers.",
                           [{"field": "build", "why": f"a system package that provides {m.group(1)}, or build options "
                                                      f"that disable that feature (cmake_args / make_args)"}],
                           {"build": {"cmake_args": ["-DWITH_FEATURE=OFF"]}}, log_tail=tail)
    if "command not found" in low or res.get("exit_code") == 127:
        return NeedsRuntime("toolchain", f"The {what} step needs a program the build workers don't have "
                                         f"(`{cmd}`).", [{"field": "runtime", "why": cmd}], log_tail=tail)
    return ImportFailure("build_failed", f"The {what} step failed (`{cmd}`, exit code {res.get('exit_code')}). "
                                         f"Last output: {tail[-600:]}", log_tail=tail)


def _copy_src(src: Path, plan: Dict[str, Any], dest: Path) -> Path:
    base = src / plan["root"] if plan.get("root") else src
    shutil.copytree(base, dest, symlinks=False, ignore=shutil.ignore_patterns(".git"))
    return dest


def _readme(base: Path) -> Optional[str]:
    for p in sorted(base.iterdir()) if base.is_dir() else []:
        if p.is_file() and re.match(r"(?i)^readme(\.(md|rst|txt|markdown))?$", p.name) and p.stat().st_size < 1_000_000:
            return p.read_text(encoding="utf-8", errors="replace")
    return None


def _env_for(plan: Dict[str, Any]) -> Dict[str, str]:
    return {k: str(v) for k, v in ((plan.get("build") or {}).get("env") or {}).items()}


def _discovered(res: Dict[str, Any]) -> Dict[str, Any]:
    if res.get("ok"):
        return res
    code = str(res.get("code") or "internal_error")
    reason = str(res.get("reason") or res.get("error") or "discovery failed")
    details = {"candidates": res["candidates"][:10]} if isinstance(res.get("candidates"), list) else {}
    if code == "needs_configuration":
        raise NeedsConfig(reason, [{"field": "operation", "why": "which function or command to call, its inputs and "
                                                                  "a sample input"}],
                          {"operation": {"kind": "function", "target": "exported_name",
                                         "positional": ["{text}"], "input_schema": {"type": "object", "properties": {
                                             "text": {"type": "string"}}, "required": ["text"]},
                                         "sample_inputs": {"text": "example"}}}, **details)
    raise ImportFailure(code, reason, **details)


# -- python -------------------------------------------------------------------------------------------
def _wheel_meta(whl: Path) -> Dict[str, Any]:
    import zipfile
    meta: Dict[str, Any] = {"requires": [], "modules": [], "scripts": {}, "name": None, "summary": ""}
    with zipfile.ZipFile(whl) as z:
        names = z.namelist()
        info = next((n for n in names if n.endswith(".dist-info/METADATA")), None)
        if info:
            text = z.read(info).decode("utf-8", "replace")
            head = text.split("\n\n", 1)[0]
            for line in head.splitlines():
                k, _, v = line.partition(": ")
                if k == "Requires-Dist":
                    meta["requires"].append(v.strip())
                elif k == "Name":
                    meta["name"] = v.strip()
                elif k == "Summary":
                    meta["summary"] = v.strip()
        eps = next((n for n in names if n.endswith(".dist-info/entry_points.txt")), None)
        if eps:
            section = None
            for line in z.read(eps).decode("utf-8", "replace").splitlines():
                line = line.strip()
                if line.startswith("["):
                    section = line.strip("[]")
                elif section == "console_scripts" and "=" in line:
                    k, _, v = line.partition("=")
                    meta["scripts"][k.strip()] = v.strip().split("[")[0].strip()
        tops = set()
        for n in names:
            first = n.split("/")[0]
            if first.endswith((".dist-info", ".data")) or first.startswith("_") or first in ("tests", "test"):
                continue
            if "/" in n and n.split("/")[0].isidentifier():
                tops.add(first)
            elif n.endswith(".py") and "/" not in n:
                tops.add(n[:-3])
        meta["modules"] = sorted(tops)
    return meta


def _deps(requires: List[str]) -> List[str]:
    out = []
    for r in requires:
        marker = r.split(";", 1)[1] if ";" in r else ""
        if "extra" in marker.replace(" ", ""):
            continue
        out.append(r)
    return out


def python_build(src: Path, plan: Dict[str, Any], out: Path, commit: str, pins: Optional[List[str]]) -> Dict[str, Any]:
    py = plan["python"]
    proj = _copy_src(src, plan, out / "src")
    site = out / "site"
    site.mkdir()
    extra = list(plan.get("extra_requirements") or [])
    if not py.get("build"):  # plain source tree: import from the folder, wheels for dependencies
        info = packaging.analyze(proj, plan.get("repo_name", ""))
        module = plan.get("package") or info.module
        if pins:
            installer.reinstall(pins, site)
        new_pins = pins or installer.install(info.dependencies + extra, site)
        return {"runtime": "python", "module": module, "paths": [str(info.root), str(site)],
                "readme": info.readme, "summary": info.description, "pins": pins or new_pins, "scripts": {}}
    buildenv, wheels = out / "buildenv", out / "wheels"
    buildenv.mkdir()
    wheels.mkdir()
    installer.install(list(py.get("build_requires") or []), buildenv)  # wheels only; skips what's installed
    version = plan.get("version") or plan.get("version_hint") or "0.0.0"
    env = {"PYTHONPATH": str(buildenv), "PYTHONNOUSERSITE": "1", "PIP_NO_INDEX": "1", "PIP_NO_CACHE_DIR": "1",
           "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_INPUT": "1",
           "SETUPTOOLS_SCM_PRETEND_VERSION": version, "PDM_BUILD_SCM_VERSION": version,
           "VERSIONINGIT_VERSION": version, "MAKEFLAGS": f"-j{JOBS}", **_env_for(plan)}
    steps = [{"argv": [sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--no-index",
                       "--wheel-dir", str(wheels), str(proj)], "cwd": str(proj)}]
    steps += [{"argv": c, "cwd": str(proj)} for c in (plan.get("build") or {}).get("commands", [])]
    _steps(steps, network=False, writable=[proj, wheels, buildenv], paths=[str(buildenv)], timeout=BUILD_TIMEOUT,
           env=env, plan=plan)
    built = sorted(wheels.glob("*.whl"))
    if not built:
        raise ImportFailure("build_failed", "The build finished but produced no wheel.")
    meta = _wheel_meta(built[0])
    if pins:
        installer.reinstall(pins, site)
        new_pins = pins
    else:
        new_pins = installer.install(_deps(meta["requires"]) + extra, site)
    installer.install_wheel(built[0], site)
    wanted = plan.get("package")
    mods = meta["modules"]
    norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())  # noqa: E731
    keys = {norm(meta.get("name")), norm(plan.get("repo_name")), norm(py.get("name"))}
    module = wanted or next((m for m in mods if norm(m) in keys), None) or (mods[0] if len(mods) == 1 else None)
    return {"runtime": "python", "module": module, "modules": mods, "paths": [str(site)], "wheel": built[0].name,
            "readme": _readme(proj), "summary": meta.get("summary") or "", "pins": new_pins,
            "scripts": meta["scripts"] or py.get("scripts") or {}}


def python_bins(build: Dict[str, Any]) -> Dict[str, List[str]]:
    out = {}
    for name, target in (build.get("scripts") or {}).items():
        mod, _, attr = target.partition(":")
        if not re.fullmatch(r"[A-Za-z_][\w.]*", mod) or not re.fullmatch(r"[A-Za-z_][\w.]*", attr or "main"):
            continue
        shim = (f"import sys; sys.argv[0] = {name!r}; from {mod} import {attr.split('.')[0]} as _f; "
                + (f"_f = _f.{'.'.join(attr.split('.')[1:])}; " if "." in attr else "") + "sys.exit(_f())")
        out[name] = [sys.executable, "-c", shim]
    return out


# -- node / frontend ----------------------------------------------------------------------------------
def _node_cmd(manager: str) -> List[str]:
    node = node_bin()
    if manager == "npm":
        cli = npm_cli()
        if not cli:
            raise NeedsRuntime("node", "npm isn't installed on the build workers.", [{"field": "runtime", "why": "npm"}])
        return [node, cli]
    corepack = Path(npm_cli() or "").parent.parent.parent / "corepack" / "dist" / "corepack.js"
    if manager in ("yarn", "pnpm") and corepack.is_file():
        return [node, str(corepack), manager]
    raise NeedsRuntime("node", f"This project uses {manager}, which isn't available on the build workers.",
                       [{"field": "runtime", "why": f"{manager} (or a package-lock.json so npm can be used)"}])


def node_build(src: Path, plan: Dict[str, Any], out: Path) -> Dict[str, Any]:
    nd = plan["node"]
    proj = _copy_src(src, plan, out / "src")
    cache = out / ".cache"
    cache.mkdir()
    env = {"npm_config_cache": str(cache), "npm_config_update_notifier": "false", "npm_config_fund": "false",
           "npm_config_audit": "false", "COREPACK_HOME": str(cache / "corepack"), "COREPACK_ENABLE_DOWNLOAD_PROMPT": "0",
           "CI": "1", "NODE_ENV": "development", **_env_for(plan)}
    if node_headers():
        env["npm_config_nodedir"] = node_headers()
    pm = _node_cmd(nd["manager"])
    if nd["manager"] == "npm":
        install = ["ci"] if nd.get("lockfile") in ("package-lock.json", "npm-shrinkwrap.json") else ["install", "--no-package-lock"]
        fetch = pm + install + ["--ignore-scripts", "--no-audit", "--no-fund", "--loglevel=error"]
    elif nd["manager"] == "pnpm":
        fetch = pm + ["install", "--frozen-lockfile" if nd.get("lockfile") else "--no-frozen-lockfile", "--ignore-scripts"]
    else:
        fetch = pm + ["install", "--frozen-lockfile" if nd.get("lockfile") else "--no-immutable", "--ignore-scripts"]
    has_deps = (proj / "package.json").is_file() and any(
        json.loads((proj / "package.json").read_text(encoding="utf-8", errors="replace")).get(k)
        for k in ("dependencies", "devDependencies", "optionalDependencies"))
    if has_deps:  # network, but no repo or dependency code runs (scripts disabled)
        try:
            _steps([{"argv": fetch, "cwd": str(proj)}], network=True, writable=[proj, cache], timeout=FETCH_TIMEOUT,
                   env=env, what="fetch")
        except ImportFailure:
            if nd["manager"] != "npm" or "ci" not in fetch:
                raise   # npm ci refuses lockfiles out of sync with package.json: resolve again instead
            _steps([{"argv": pm + ["install", "--no-package-lock", "--ignore-scripts", "--no-audit", "--no-fund",
                                   "--loglevel=error"], "cwd": str(proj)}],
                   network=True, writable=[proj, cache], timeout=FETCH_TIMEOUT, env=env, what="fetch")
    steps = []
    if has_deps:  # now run the dependencies' install scripts (native addons), offline
        steps.append({"argv": _node_cmd("npm") + ["rebuild", "--foreground-scripts"], "cwd": str(proj)})
    script = (plan.get("build") or {}).get("script") or nd.get("build_script")
    if script:
        steps.append({"argv": pm + ["run", script], "cwd": str(proj)})
    steps += [{"argv": c, "cwd": str(proj)} for c in (plan.get("build") or {}).get("commands", [])]
    if steps:
        _steps(steps, network=False, writable=[proj, cache], timeout=BUILD_TIMEOUT, env=env, plan=plan)
    shutil.rmtree(cache, ignore_errors=True)
    pkg = json.loads((proj / "package.json").read_text(encoding="utf-8", errors="replace"))
    return {"runtime": plan["runtime"], "package_dir": str(proj), "main": nd.get("main"), "name": pkg.get("name"),
            "bin": nd.get("bin") or {}, "readme": _readme(proj), "summary": nd.get("description") or "",
            "typescript": bool(nd.get("typescript"))}


def node_bins(build: Dict[str, Any]) -> Dict[str, List[str]]:
    proj = Path(build["package_dir"])
    return {name: [node_bin(), str((proj / rel).resolve())] for name, rel in (build.get("bin") or {}).items()
            if isinstance(rel, str) and (proj / rel).is_file()}


def _shipped_assets(src: Path, plan: Dict[str, Any]) -> bool:
    base = src / plan["root"] if plan.get("root") else src
    nd = plan.get("node") or {}
    main = nd.get("style") or nd.get("main")
    if isinstance(main, str) and (base / main).is_file() and main.endswith((".css", ".scss", ".sass", ".less")):
        return True
    return any((base / d).is_dir() and any((base / d).rglob("*.css")) for d in ("dist", "css", "build"))


def collect_assets(build: Dict[str, Any], plan: Dict[str, Any], out: Path) -> Dict[str, Any]:
    proj = Path(build["package_dir"])
    globs = plan.get("assets") or []
    pkg = json.loads((proj / "package.json").read_text(encoding="utf-8", errors="replace"))
    main = next((v for v in (pkg.get("style"), pkg.get("sass"), pkg.get("main")) if isinstance(v, str)), None)
    if not globs:
        globs = [g for d in ("dist", "build", "css", "lib") if (proj / d).is_dir()
                 for g in (f"{d}/**/*.css", f"{d}/**/*.min.js", f"{d}/**/*.js", f"{d}/**/*.woff2", f"{d}/**/*.svg")]
        if main:
            globs.append(main)
    files = []
    for p in sorted(proj.rglob("*")):
        rel = p.relative_to(proj).as_posix()
        if p.is_file() and "node_modules" not in p.parts and any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(rel, g.replace("**/", "")) for g in globs):
            files.append((rel, p))
    if not files:
        raise NeedsConfig("The build produced no assets Cognex recognizes (CSS, JavaScript bundles, fonts, SVG in "
                          "dist/, build/, css/ or the package's style/main file).",
                          [{"field": "assets", "why": "glob patterns of the built files to publish"}],
                          {"assets": ["dist/*.css"]})
    total, kept = 0, []
    dest = out / "assets"
    for rel, p in files[:MAX_ASSETS]:
        total += p.stat().st_size
        if total > MAX_ASSET_BYTES:
            break
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, dest / rel)
        kept.append({"path": rel, "bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
                     "content_type": mimetypes.guess_type(rel)[0] or "application/octet-stream"})
    primary = next((f["path"] for f in kept if main and f["path"] == main.lstrip("./")), None) or \
        next((f["path"] for f in kept if f["path"].endswith(".min.css")), None) or \
        next((f["path"] for f in kept if f["path"].endswith(".css")), kept[0]["path"])
    return {"assets_dir": str(dest), "assets": kept, "primary": primary}


# -- native (C / C++) ---------------------------------------------------------------------------------
def native_build(src: Path, plan: Dict[str, Any], out: Path) -> Dict[str, Any]:
    nat = plan.get("native") or {}
    proj = _copy_src(src, plan, out / "src")
    bcfg = plan.get("build") or {}
    system = bcfg.get("system") or nat.get("system")
    builddir = proj / "_cognex_build"
    if bcfg.get("commands"):
        steps = [{"argv": c, "cwd": str(proj)} for c in bcfg["commands"]]
    elif system == "cmake":
        if not tool("cmake"):
            raise NeedsRuntime("native", "CMake isn't installed on the build workers.", [{"field": "runtime", "why": "cmake"}])
        steps = [{"argv": [tool("cmake"), "-S", str(proj), "-B", str(builddir), "-DCMAKE_BUILD_TYPE=Release",
                           "-DBUILD_TESTING=OFF", *bcfg.get("cmake_args", [])], "cwd": str(proj)},
                 {"argv": [tool("cmake"), "--build", str(builddir), "--parallel", JOBS], "cwd": str(proj)}]
    elif system == "autotools":
        steps = [{"argv": [tool("autoreconf") or "autoreconf", "-fi"], "cwd": str(proj)}] if nat.get("autoreconf") \
            and not (proj / "configure").is_file() else []
        steps += [{"argv": ["sh", "./configure", *bcfg.get("cmake_args", [])], "cwd": str(proj)},
                 {"argv": [tool("make") or "make", f"-j{JOBS}", *bcfg.get("make_args", [])], "cwd": str(proj)}]
    else:
        steps = [{"argv": [tool("make") or "make", f"-j{JOBS}", *bcfg.get("make_args", [])], "cwd": str(proj)}]
    if not (tool("cc") or tool("gcc")) and not bcfg.get("commands"):
        raise NeedsRuntime("native", "No C/C++ compiler is installed on the build workers.",
                           [{"field": "runtime", "why": "a C/C++ toolchain (build-essential)"}])
    started = time.time()
    _steps(steps, network=False, writable=[proj], timeout=BUILD_TIMEOUT,
           env={"MAKEFLAGS": f"-j{JOBS}", **_env_for(plan)}, plan=plan)
    wanted = plan.get("executables")
    if wanted:
        exes = [proj / w for w in wanted if (proj / w).is_file()]
        if len(exes) != len(wanted):
            raise NeedsConfig("Some configured executables weren't built: "
                              + ", ".join(w for w in wanted if not (proj / w).is_file()),
                              [{"field": "executables", "why": "paths (relative to the project) of built programs"}],
                              {"executables": ["_cognex_build/tool"]})
    else:
        exes = []
        for p in proj.rglob("*"):
            rel = p.relative_to(proj).as_posix()
            if not p.is_file() or "CMakeFiles" in p.parts or p.suffix in (".so", ".a", ".o", ".dylib", ".sh", ".py") \
                    or re.search(r"\.so(\.\d+)+$", p.name) or ".libs" in p.parts or ".deps" in p.parts:
                continue
            if p.stat().st_mtime < started - 1 or not os.access(p, os.X_OK):
                continue
            with open(p, "rb") as fh:
                if fh.read(4) != b"\x7fELF":
                    continue
            if re.search(r"(?i)(^|[/_-])(test|tests|bench|benchmark|example|fuzz)", rel):
                continue
            exes.append(p)
    libs = [p.relative_to(proj).as_posix() for p in proj.rglob("*") if (p.suffix in (".so", ".a") or
            re.search(r"\.so(\.\d+)+$", p.name)) and "CMakeFiles" not in p.parts][:20]
    return {"runtime": "native", "package_dir": str(proj), "executables": {p.name: str(p) for p in exes[:20]},
            "libraries": libs, "readme": _readme(proj), "summary": ""}


# -- HTTP services ------------------------------------------------------------------------------------
def service_build(src: Path, plan: Dict[str, Any], out: Path, pins: Optional[List[str]]) -> Dict[str, Any]:
    from app.imports import services
    svc = plan["service"]
    cfg = (plan.get("config") or {}).get("service") or {}
    if svc["runtime"] == "node":
        info = node_build(src, {**plan, "runtime": "node"}, out)
        app_dir = Path(info["package_dir"])
        start = cfg.get("start") or services.node_start(app_dir, plan["node"])
        if not start:
            raise NeedsConfig("Cognex couldn't tell how to start this Node.js server (no start script or main file).",
                              [{"field": "service.start", "why": "the command that starts the server on $PORT"}],
                              {"service": {"start": ["node", "server.js"]}})
        return {**info, "runtime": "service", "service_runtime": "node", "app_dir": str(app_dir), "paths": [],
                "start": start, "env": cfg.get("env") or {}, "scratch_root": str(_scratch())}
    proj = _copy_src(src, plan, out / "src")
    site = out / "site"
    site.mkdir()
    reqs = list((plan.get("python") or {}).get("requires") or [])
    if not reqs and (proj / "setup.py").is_file():
        reqs = packaging._setup_py((proj / "setup.py").read_text(encoding="utf-8", errors="replace")).get("install_requires") or []
        reqs = reqs if isinstance(reqs, list) else []
    reqs = packaging._clean_reqs([r for r in reqs if isinstance(r, str) and not r.startswith("python")])
    reqs += list(plan.get("extra_requirements") or [])
    if pins:
        installer.reinstall(pins, site)
    new_pins = pins or installer.install(reqs, site)
    app = svc.get("app") or {}
    app_dir = proj / app.get("src_root", "") if app.get("src_root") else proj
    return {"runtime": "service", "service_runtime": "python", "app_dir": str(app_dir), "paths": [str(site)],
            "pins": new_pins, "start": cfg.get("start") or services.python_start(app), "env": cfg.get("env") or {},
            "readme": _readme(proj), "summary": "", "scratch_root": str(_scratch()), "app": app}


# -- discovery: one entry point per runtime -----------------------------------------------------------
def discover(plan: Dict[str, Any], build: Dict[str, Any], entry_point: Optional[str]) -> Dict[str, Any]:
    """The operation to register: {"spec": how to call it, input_schema, sample_inputs, sample_output, ...}."""
    op = plan.get("operation")
    entry = entry_point or plan.get("entry_point")
    runtime = plan["runtime"]
    if runtime == "frontend" or (op and op["kind"] == "asset"):
        assets = build["assets"]
        paths = [a["path"] for a in assets]
        rebuilt = build.get("rebuilt", True)
        spec = {"kind": "asset", "assets_dir": build["assets_dir"], "assets": assets,
                "assets_source": "rebuilt from source" if rebuilt else
                "shipped with the package (the build failed: " + str(build.get("build_error") or "")[:200] + ")"}
        sample = {"path": build["primary"]}
        return {"spec": spec, "function": "assets", "summary": (build.get("summary") or "Built frontend assets") ,
                "input_schema": {"type": "object", "properties": {"path": {"enum": paths, "description":
                                 "Asset to return; omit to list all assets"}}},
                "sample_inputs": sample, "sample_output": call_asset(spec, sample), "signature": "assets(path?)"}
    if op and op["kind"] == "command" or (runtime == "native"):
        return _discover_command(plan, build, op, entry)
    if runtime == "python":
        if op and op["kind"] == "function":
            return _verify_configured(plan, build, op, "chain",
                                      {"kind": "chain", "target": op["target"], "args": op.get("args", {}),
                                       "positional": op.get("positional", []), "then": op.get("then", [])})
        if not build.get("module"):
            mods = build.get("modules") or []
            raise NeedsConfig("The built package contains several top-level modules (" + ", ".join(mods[:8])
                              + "); Cognex couldn't tell which one is the library.",
                              [{"field": "package", "why": "the module to import"}], {"package": mods[0] if mods else "name"},
                              candidates=mods[:10])
        res = sandbox.run("discover", {"module": build["module"], "repo_name": plan.get("repo_name", ""),
                                       "readme": build.get("readme"), "entry_point": entry},
                          paths=build["paths"], timeout=DISCOVER_TIMEOUT, scratch_root=_scratch(), memory_mb=_mem(plan))
        if not res.get("ok") and res.get("code") in ("no_callable_function", "multiple_entry_points",
                                                       "unsupported_input") and build.get("scripts") and not entry:
            try:  # a command-line tool: fall back to its documented commands
                return _discover_command(plan, build, None, None)
            except ImportFailure:
                pass
        if not res.get("ok") and res.get("code") in ("no_callable_function", "unsupported_input", "unsupported_output",
                                                       "multiple_entry_points"):
            raise NeedsConfig(str(res.get("reason")), [{"field": "operation" if res.get("code") != "multiple_entry_points"
                                                        else "entry_point",
                                                        "why": "which function to call and how to turn JSON into its "
                                                               "arguments (and its result into JSON)"}],
                              {"operation": {"kind": "function", "target": f"{build['module']}:function_name",
                                             "args": {"param": "{input_name}"}, "then": [],
                                             "input_schema": {"type": "object", "properties": {"input_name": {}},
                                                              "required": ["input_name"]},
                                             "sample_inputs": {"input_name": "..."}}} if res.get("code") != "multiple_entry_points"
                              else {"entry_point": (res.get("candidates") or ["name"])[0]},
                              code=str(res.get("code")),
                              **({"candidates": res["candidates"][:10]} if isinstance(res.get("candidates"), list) else {}))
        res = _discovered(res)
        return {"spec": {"kind": "function", "module": res["module"], "function": res["function"], "params": res["params"]},
                "function": res["function"], "summary": res.get("summary") or build.get("summary") or "",
                "input_schema": res["input_schema"], "sample_inputs": res["sample_inputs"],
                "sample_output": res["sample_output"], "signature": res.get("signature"), "_raw": res}
    if runtime == "node":
        if op and op["kind"] == "function":
            spec = {"kind": "node", "function": op["target"].split(".")[0], "path": op["target"].split(".")[1:],
                    "positional": op.get("positional", []), "package_dir": build["package_dir"], "main": build["main"],
                    "typescript": build.get("typescript")}
            return _verify_configured(plan, build, op, "node", spec)
        res = sandbox.run("node", _node_payload(build, {"mode": "discover", "readme": build.get("readme"),
                                                         "entry_point": entry, "repo_name": plan.get("repo_name")}),
                          paths=[], timeout=DISCOVER_TIMEOUT, scratch_root=_scratch(), memory_mb=max(8192, _mem(plan)))
        if not res.get("ok") and build.get("bin") and not entry:
            try:
                return _discover_command(plan, build, None, None)
            except ImportFailure:
                pass
        res = _discovered(res)
        return {"spec": {"kind": "node", "function": res["function"], "params": res["params"],
                         "package_dir": build["package_dir"], "main": res.get("entry") or build["main"],
                         "typescript": build.get("typescript")},
                "function": res["function"] if res["function"] != "default" else (build.get("name") or "default"),
                "summary": build.get("summary") or "", "input_schema": res["input_schema"],
                "sample_inputs": res["sample_inputs"], "sample_output": res["sample_output"],
                "signature": f"{res['function']}({', '.join(res['params'])})"}
    if runtime == "service":
        from app.imports import services
        res = services.discover(plan, build, entry, memory_mb=max(4096, _mem(plan)))
        res["function"] = re.sub(r"[^A-Za-z0-9_]+", "_", res["function"]).strip("_")[:60] or "endpoint"
        return res
    raise NeedsRuntime(runtime, f"Cognex has no {runtime} runtime yet.", [{"field": "runtime", "why": runtime}])


def _mem(plan_or_spec: Dict[str, Any]) -> int:
    return profiles.get(plan_or_spec.get("profile", "standard"))["call_memory_mb"]


def _node_payload(build: Dict[str, Any], extra: Dict[str, Any]) -> Dict[str, Any]:
    return {"node": node_bin(), "app_dir": str(ROOT), "package_dir": build["package_dir"], "main": build.get("main"),
            "package_name": build.get("name"), "typescript": build.get("typescript"), **extra}


def bins_for(build: Dict[str, Any]) -> Dict[str, List[str]]:
    if build["runtime"] == "python":
        return python_bins(build)
    if build["runtime"] in ("node", "frontend"):
        return node_bins(build)
    return {name: [path] for name, path in (build.get("executables") or {}).items()}


def _cmd_env(build: Dict[str, Any]) -> Dict[str, str]:
    return {"PYTHONPATH": os.pathsep.join(build["paths"])} if build["runtime"] == "python" else {}


def _discover_command(plan, build, op, entry) -> Dict[str, Any]:
    bins = bins_for(build)
    if op and op["kind"] == "command":
        spec = {"kind": "command", "argv": op["argv"], "stdin": op.get("stdin"), "output": op.get("output", "stdout"),
                "bins": bins, "env": _cmd_env(build)}
        return _verify_configured(plan, build, op, "command", spec)
    if not bins:
        libs = build.get("libraries") or []
        raise NeedsConfig("The build produced no executable program to run" + (f" (only libraries: {', '.join(libs[:5])})"
                                                                               if libs else "") + ".",
                          [{"field": "executables", "why": "the built program(s) to expose"}] +
                          ([{"field": "runtime", "why": "calling a C/C++ library directly needs language bindings "
                                                        "(a new runtime); a command-line program works today"}] if libs else []),
                          {"executables": ["_cognex_build/program"]})
    res = sandbox.run("cli_discover", {"bins": bins, "readme": build.get("readme"), "entry_point": entry,
                                       "env": _cmd_env(build), "repo_name": plan.get("repo_name")},
                      paths=[], timeout=DISCOVER_TIMEOUT, scratch_root=_scratch(), memory_mb=_mem(plan))
    if not res.get("ok"):
        name = sorted(bins)[0]
        raise NeedsConfig(str(res.get("reason")), [{"field": "operation", "why": f"the command line to run "
                                                     f"({', '.join(sorted(bins))}) with a sample input"}],
                          {"operation": {"kind": "command", "argv": [f"{{bin:{name}}}", "{args...}"], "stdin": "{stdin}",
                                         "input_schema": {"type": "object", "properties": {
                                             "args": {"type": "array", "items": {"type": "string"}},
                                             "stdin": {"type": "string"}}},
                                         "sample_inputs": {"args": ["--version"]}}},
                          executables=sorted(bins), tried=res.get("tried") or [])
    spec = {"kind": "command", "argv": [f"{{bin:{res['bin']}}}", "{args...}"], "stdin": "{stdin}",
            "output": res.get("output") or "stdout", "bins": {res["bin"]: bins[res["bin"]]}, "env": _cmd_env(build)}
    return {"spec": spec, "function": res["bin"], "summary": build.get("summary") or f"Run the {res['bin']} command",
            "input_schema": {"type": "object", "properties": {
                "args": {"type": "array", "items": {"type": "string"}, "description": f"Command-line arguments for {res['bin']}"},
                "stdin": {"type": "string", "description": "Text sent to standard input"}}},
            "sample_inputs": res["sample_inputs"], "sample_output": res["sample_output"],
            "signature": f"{res['bin']} [args...] < stdin" + (" (standard-input filter)" if res.get("filter") else "")}


def _verify_configured(plan, build, op, mode: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    """A configured operation: run its sample input through the production path and check it."""
    if "sample_inputs" not in op or "input_schema" not in op:
        raise NeedsConfig("The configured operation needs an input_schema and sample_inputs to test it with.",
                          [{"field": "operation.input_schema", "why": "the JSON inputs"},
                           {"field": "operation.sample_inputs", "why": "a real input to test before Ready"}],
                          {"operation": {**op, "input_schema": {"type": "object", "properties": {}},
                                         "sample_inputs": {}}})
    out = call({**spec, "profile": plan.get("profile", "standard")}, build, op["sample_inputs"], DISCOVER_TIMEOUT)
    if not out.get("ok"):
        raise ImportFailure(str(out.get("code") or "test_failed") if out.get("code") in
                            ("unsupported_output", "timeout") else "test_failed",
                            f"Test call with the configured sample inputs failed: {out.get('error') or out.get('reason')}"[:900])
    if "expected_output" in op:
        from app.imports.runner import _close
        got = out["output"]
        want = op["expected_output"]
        if not (_close(got, want) or _close(got.get("result"), want) or _close(got.get("stdout", "").strip(), want)):
            raise ImportFailure("test_failed", f"Test call returned {json.dumps(got)[:200]} but the configuration "
                                               f"expects {json.dumps(want)[:200]}.")
    fn = op.get("name") or (spec.get("function") if mode == "node" else str(op.get("target", "run")).split(":")[-1].split(".")[-1]
                            if mode == "chain" else "run")
    return {"spec": spec, "function": fn, "summary": op.get("summary") or build.get("summary") or f"Run {fn}",
            "input_schema": op["input_schema"], "sample_inputs": op["sample_inputs"], "sample_output": out["output"],
            "signature": op.get("target") or " ".join(op.get("argv", [])), "long_running": op.get("long_running"),
            "timeout": op.get("timeout")}


# -- calling ------------------------------------------------------------------------------------------
def call_asset(spec: Dict[str, Any], inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Assets are static files: read directly (nothing executes)."""
    import base64
    files = {a["path"]: a for a in spec["assets"]}
    path = inputs.get("path")
    if not path:
        return {"assets": list(files.values())}
    if path not in files:
        from app.errors import OperationError
        raise OperationError(f"unknown asset '{path}'")
    data = (Path(spec["assets_dir"]) / path).read_bytes()
    meta = files[path]
    text = meta["content_type"].startswith("text/") or path.endswith((".css", ".js", ".svg", ".map", ".json"))
    return {**meta, **({"content": data.decode("utf-8", "replace")} if text else
                       {"content_base64": base64.b64encode(data).decode()})}


def call(spec: Dict[str, Any], build: Dict[str, Any], inputs: Dict[str, Any], timeout: float,
         on_start=None) -> Dict[str, Any]:
    """One production call (untrusted code runs in a fresh sandbox): {"ok", "output"} or an error."""
    kind = spec.get("kind", "function")
    if kind == "asset":
        return {"ok": True, "output": call_asset(spec, inputs)}
    mem = _mem(spec)
    if kind == "function":
        return sandbox.run("call", {"spec": spec, "inputs": inputs}, paths=build["paths"], timeout=timeout,
                           scratch_root=_scratch(), on_start=on_start, memory_mb=mem)
    if kind == "chain":
        return sandbox.run("chain", {"spec": spec, "inputs": inputs}, paths=build["paths"], timeout=timeout,
                           scratch_root=_scratch(), on_start=on_start, memory_mb=mem)
    if kind == "node":
        return sandbox.run("node", _node_payload(build, {"mode": "call", "spec": spec, "inputs": inputs,
                                                         "timeout": timeout}),
                           paths=[], timeout=timeout + 5, scratch_root=_scratch(), on_start=on_start,
                           memory_mb=max(8192, mem))
    if kind == "command":
        return sandbox.run("command", {"spec": spec, "inputs": inputs, "timeout": timeout}, paths=[],
                           timeout=timeout + 5, scratch_root=_scratch(), on_start=on_start, memory_mb=mem)
    if kind == "http":
        from app.imports import services
        return services.call(spec, build, inputs, timeout, on_start=on_start, memory_mb=mem)
    return {"ok": False, "error_type": "operation_failed", "error": f"unknown operation kind {kind}"}


def build(src: Path, plan: Dict[str, Any], commit: str, pins: Optional[List[str]] = None) -> Tuple[str, Dict[str, Any], bool]:
    """Build (or reuse) the project; returns (build key, build info, reused)."""
    key = build_key(plan, commit, src)

    def make(tmp: Path) -> Dict[str, Any]:
        rt = plan["runtime"]
        if rt == "python":
            info = python_build(src, plan, tmp, commit, pins)
        elif rt in ("node", "frontend"):
            try:
                info = node_build(src, plan, tmp)
            except ImportFailure as exc:
                if rt != "frontend" or not _shipped_assets(src, plan):
                    raise
                # the build didn't run here, but the package ships its built CSS: publish that, and say so
                shutil.rmtree(tmp / "src", ignore_errors=True)
                proj = _copy_src(src, plan, tmp / "src")
                info = {"runtime": "frontend", "package_dir": str(proj), "main": plan["node"].get("main"),
                        "name": plan["node"].get("name"), "bin": {}, "readme": _readme(proj),
                        "summary": plan["node"].get("description") or "", "typescript": False,
                        "rebuilt": False, "build_error": exc.reason[:300]}
            if rt == "frontend" or (plan.get("operation") or {}).get("kind") == "asset":
                info.update(collect_assets(info, plan, tmp))
        elif rt == "native":
            info = native_build(src, plan, tmp)
        elif rt == "service":
            info = service_build(src, plan, tmp, pins)
        else:
            raise NeedsRuntime(rt, f"Cognex has no {rt} runtime yet.", [{"field": "runtime", "why": rt}])
        info["key"] = key
        return info

    _, info, reused = ensure_build(key, make)
    return key, info, reused
