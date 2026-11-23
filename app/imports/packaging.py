"""Static look at a downloaded repo: which Python package to import, what it depends on, and
whether it can be supported (pure Python + dependencies available as prebuilt wheels).

Trusted code that only parses files (TOML, and setup.py as an AST); it never executes them.
"""
from __future__ import annotations

import ast
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from app.imports.github import ImportFailure

MAX_PARSE = 512 * 1024
COMPILED_SUFFIXES = {".c", ".cc", ".cpp", ".cxx", ".pyx", ".pxd", ".rs", ".f", ".f90", ".cu", ".go"}
NATIVE_BACKENDS = ("maturin", "scikit_build", "scikit-build", "mesonpy", "meson-python", "setuptools_rust",
                   "cmake", "pybind11")
SKIP_DIRS = {"tests", "test", "docs", "doc", "examples", "example", "benchmarks", "bench", "scripts", "build",
             "dist", ".git", ".github", "node_modules", "venv", ".venv", "site-packages"}
REQ_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._ -]+\])?\s*([<>=!~][^;#]*)?(;[^#]*)?$")


@dataclass
class PackageInfo:
    module: str                      # top-level import name
    root: Path                       # directory to put on sys.path
    dependencies: List[str] = field(default_factory=list)
    description: str = ""
    readme: Optional[str] = None     # README text (examples are harvested in the sandbox)
    name: Optional[str] = None       # distribution name, if declared


def _read(p: Path) -> Optional[str]:
    if p.is_file() and p.stat().st_size <= MAX_PARSE:
        return p.read_text(encoding="utf-8", errors="replace")
    return None


def _setup_py(text: str) -> dict:
    """Literal keyword arguments of setup(...) in setup.py, read from its AST (not executed)."""
    out: dict = {}
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "setup":
            for kw in node.keywords:
                if kw.arg in ("name", "packages", "py_modules", "install_requires", "ext_modules", "package_dir",
                              "description", "cffi_modules", "rust_extensions"):
                    try:
                        out[kw.arg] = ast.literal_eval(kw.value)
                    except (ValueError, SyntaxError, RecursionError):
                        out[kw.arg] = "<dynamic>"
    return out


def _clean_reqs(reqs) -> List[str]:
    out = []
    for r in reqs or []:
        if not isinstance(r, str):
            continue
        r = r.split("#", 1)[0].strip()
        if not r or r.startswith("-"):
            continue
        if "://" in r or " @ " in r or not REQ_RE.match(r):
            raise ImportFailure("unsupported_dependency",
                                f"Dependency '{r[:100]}' isn't a plain package requirement (URLs, local paths and "
                                f"pip options aren't supported).")
        marker = r.split(";", 1)[1] if ";" in r else ""
        if "extra ==" in marker.replace("'", "").replace('"', "") or "extra==" in marker:
            continue  # optional extras aren't installed
        out.append(r)
    return out


def _find_compiled(root: Path, module_dir: Path) -> Optional[str]:
    for p in module_dir.rglob("*") if module_dir.is_dir() else []:
        if p.suffix.lower() in COMPILED_SUFFIXES and not (set(p.relative_to(root).parts) & {"tests", "test", "docs"}):
            return str(p.relative_to(root)).replace("\\", "/")
    return None


def analyze(repo: Path, repo_name: str) -> PackageInfo:
    pyproject = _read(repo / "pyproject.toml")
    setup_py = _read(repo / "setup.py")
    setup_cfg = _read(repo / "setup.cfg")
    meta: dict = {}
    deps: List[str] = []
    name = description = None
    if pyproject:
        try:
            data = tomllib.loads(pyproject)
        except tomllib.TOMLDecodeError:
            data = {}
        backend = str(data.get("build-system", {}).get("build-backend", "")) + " " + \
            " ".join(map(str, data.get("build-system", {}).get("requires", [])))
        if any(b in backend.lower() for b in NATIVE_BACKENDS):
            raise ImportFailure("unsupported_build", "The package is built with compiled native code "
                                                     f"({backend.strip()[:80]}); Cognex imports pure-Python packages.")
        proj = data.get("project", {})
        name, description = proj.get("name"), proj.get("description")
        deps = _clean_reqs(proj.get("dependencies"))
        poetry = data.get("tool", {}).get("poetry", {})
        if not proj and poetry:
            name, description = poetry.get("name"), poetry.get("description")
            deps = [f"{k}{'' if v in ('*', None) else ''}" for k, v in poetry.get("dependencies", {}).items()
                    if k.lower() != "python" and isinstance(k, str)]
    if setup_py:
        meta = _setup_py(setup_py)
        if meta.get("ext_modules") or meta.get("cffi_modules") or meta.get("rust_extensions"):
            raise ImportFailure("unsupported_build", "setup.py builds compiled extensions; Cognex imports "
                                                     "pure-Python packages.")
        name = name or (meta.get("name") if isinstance(meta.get("name"), str) else None)
        description = description or (meta.get("description") if isinstance(meta.get("description"), str) else None)
        if not deps and isinstance(meta.get("install_requires"), list):
            deps = _clean_reqs(meta["install_requires"])
    if setup_cfg and not deps:
        m = re.search(r"install_requires\s*=\s*\n((?:[ \t]+\S.*\n?)+)", setup_cfg)
        if m:
            deps = _clean_reqs([l.strip() for l in m.group(1).splitlines()])
    if not deps and not pyproject and not setup_py and (repo / "requirements.txt").is_file():
        deps = _clean_reqs((_read(repo / "requirements.txt") or "").splitlines())

    # Which module to import: declared packages, then src/ or top-level packages, then a lone module.
    candidates = []
    for base in (repo / "src", repo):
        if base.is_dir():
            for d in sorted(base.iterdir()):
                if d.is_dir() and (d / "__init__.py").is_file() and d.name not in SKIP_DIRS \
                        and d.name.isidentifier() and not d.name.startswith((".", "_")):
                    candidates.append((d.name, base))
    declared = meta.get("packages") if isinstance(meta.get("packages"), list) else []
    py_modules = meta.get("py_modules") if isinstance(meta.get("py_modules"), list) else []
    norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())  # noqa: E731
    wanted = [norm(x) for x in (name, repo_name, repo_name.removeprefix("python-").removeprefix("py"),
                                repo_name.removesuffix("-python").removesuffix(".py"))]
    module = root = None
    for mod, base in candidates:
        if mod in [p.split(".")[0] for p in declared if isinstance(p, str)] or norm(mod) in wanted:
            module, root = mod, base
            break
    if module is None and len(candidates) == 1:
        module, root = candidates[0]
    if module is None:
        singles = [repo / f"{m}.py" for m in py_modules if isinstance(m, str)] or \
                  [p for p in repo.glob("*.py") if norm(p.stem) in wanted]
        if singles and singles[0].is_file():
            module, root = singles[0].stem, repo
    if module is None:
        if any(repo.rglob("*.py")):
            if len(candidates) > 1:
                raise ImportFailure("multiple_packages", "The repository contains several Python packages ("
                                    + ", ".join(m for m, _ in candidates[:6]) + "); Cognex couldn't tell which one "
                                    "is the library.")
            raise ImportFailure("no_python_package", "No importable Python package or module was found "
                                                     "(expected a folder with __init__.py, or a module named "
                                                     "after the repository).")
        langs = sorted({p.suffix for p in repo.rglob("*") if p.suffix in {".js", ".ts", ".rs", ".go", ".java",
                                                                            ".cpp", ".c", ".jl", ".r", ".R"}})
        raise ImportFailure("unsupported_language", "Cognex imports Python repositories; this one contains "
                            + (", ".join(langs) if langs else "no Python code") + ".")
    compiled = _find_compiled(repo, root / module)
    if compiled:
        raise ImportFailure("unsupported_build", f"The package contains compiled source ({compiled}); Cognex "
                                                 "imports pure-Python packages.")
    readme = next((p for p in sorted(repo.iterdir()) if p.is_file() and re.match(r"(?i)^readme(\.(md|rst|txt))?$", p.name)),
                  None)
    readme_text = _read(readme) if readme else None
    if not description and readme_text:
        for line in readme_text.splitlines():
            s = line.strip().strip("*_")
            if s and not s.startswith(("#", "[", "!", "<", "=", "-", "`", ">", "|")) and len(s) > 20:
                description = s[:300]
                break
    return PackageInfo(module=module, root=root, dependencies=deps, description=description or "",
                       readme=readme_text, name=name)
