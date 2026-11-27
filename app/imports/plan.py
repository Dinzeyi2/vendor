"""Build plans: how a repository is built and run, detected from its files or set by a saved
repo configuration (the same format). Trusted code: it only parses files, never executes them.

A plan names one runtime (the backend code that knows how to fetch, build, discover and call):

  python    pyproject.toml / setup.py / setup.cfg packages, built into a wheel from source
            (PEP 517 backend, compiled extensions included), or plain source folders
  node      JavaScript / TypeScript packages (package.json + npm / yarn / pnpm lockfile)
  native    C / C++ projects built with CMake, a configure script or a Makefile
  frontend  CSS / frontend packages built into static assets
  service   HTTP services: a Python ASGI/WSGI app (FastAPI, Flask...) or a Node.js server
  container Dockerfile / compose services                     (no runtime on this server yet)
  conda     conda / pixi environments                          (no runtime on this server yet)

Command-line tools are an operation kind any runtime can produce (Python console scripts, npm
`bin` entries, built executables). Adding a repo is normally detection + a repo configuration;
backend code is only for a new runtime.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import re
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Optional

import jsonschema

from app.imports.github import ImportFailure

RUNTIMES = ("python", "node", "native", "frontend", "service", "container", "conda")
AVAILABLE = ("python", "node", "native", "frontend", "service")
MAX_PARSE = 512 * 1024
IDENT = r"^[A-Za-z_][A-Za-z0-9_]{0,99}$"

_OPERATION = {
    "type": "object", "additionalProperties": False, "required": ["kind"],
    "properties": {
        "kind": {"enum": ["function", "command", "asset", "http"]},
        "method": {"enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},      # http
        "path": {"type": "string", "pattern": "^/"},                         # http
        "name": {"type": "string", "pattern": r"^[a-z][a-z0-9_]{0,59}$"},
        "summary": {"type": "string", "minLength": 10, "maxLength": 300},
        # function: "module:attr.path" (Python) or "export.path" (JavaScript)
        "target": {"type": "string", "minLength": 1, "maxLength": 300},
        "args": {"type": "object"},          # keyword arguments: {"param": "{input}" or a literal}
        "positional": {"type": "array"},     # positional arguments, same placeholders
        "then": {"type": "array", "maxItems": 10, "items": {
            "type": "object", "additionalProperties": False,
            "properties": {"attr": {"type": "string"}, "call": {"type": "string"},
                           "args": {"type": "object"}, "positional": {"type": "array"}}}},
        # command: argv template ({input}, {input...}, {file:input}, {bin:name}, {outdir}), stdin, output
        "argv": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"type": "string"}},
        "stdin": {"type": "string"},
        "output": {"type": "string", "pattern": r"^(stdout|stdout_json|file:.+|file_base64:.+)$"},
        "input_schema": {"type": "object"},
        "sample_inputs": {"type": "object"},
        "expected_output": {},
        "long_running": {"type": "boolean"},
        "timeout": {"type": "object", "additionalProperties": False, "required": ["default", "max"],
                    "properties": {"default": {"type": "number", "exclusiveMinimum": 0, "maximum": 3600},
                                   "max": {"type": "number", "exclusiveMinimum": 0, "maximum": 3600}}},
    },
}

# A saved repo configuration: every field optional; it overrides what detection found.
CONFIG_SCHEMA: Dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object", "additionalProperties": False,
    "properties": {
        "runtime": {"enum": list(RUNTIMES)},
        "profile": {"enum": ["standard", "large"]},     # worker profile (normally chosen from measurements)
        "root": {"type": "string", "pattern": r"^(?!/)(?!.*\.\.)[A-Za-z0-9._/-]{0,200}$"},
        "package": {"type": "string", "minLength": 1, "maxLength": 200},
        "entry_point": {"type": "string", "pattern": r"^(?:[A-Za-z_$][A-Za-z0-9_$.]{0,199}|(?:GET|POST|PUT|PATCH|DELETE) /[^\s]{0,200})$"},   # function, export, command or "POST /path"
        "service": {"type": "object", "additionalProperties": False, "properties": {
            "start": {"type": "array", "minItems": 1, "maxItems": 50, "items": {"type": "string"}},  # {port} placeholder
            "env": {"type": "object", "additionalProperties": {"type": "string"}}}},
        "version": {"type": "string", "pattern": r"^[0-9][0-9A-Za-z.+-]{0,60}$"},
        "extra_requirements": {"type": "array", "maxItems": 50, "items": {"type": "string", "maxLength": 200}},
        "build": {
            "type": "object", "additionalProperties": False,
            "properties": {
                "commands": {"type": "array", "maxItems": 20,
                             "items": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"type": "string"}}},
                "script": {"type": "string", "maxLength": 100},          # npm script to run
                "system": {"enum": ["cmake", "make", "autotools"]},
                "cmake_args": {"type": "array", "maxItems": 50, "items": {"type": "string"}},
                "make_args": {"type": "array", "maxItems": 50, "items": {"type": "string"}},
                "env": {"type": "object", "additionalProperties": {"type": "string"}},
            }},
        "executables": {"type": "array", "maxItems": 50, "items": {"type": "string"}},
        "assets": {"type": "array", "maxItems": 50, "items": {"type": "string"}},
        "operation": _OPERATION,
        "notes": {"type": "string", "maxLength": 2000},
    },
}


def validate_config(config: Dict[str, Any]) -> None:
    try:
        jsonschema.validate(config, CONFIG_SCHEMA)
        op = config.get("operation") or {}
        if op.get("input_schema"):
            jsonschema.Draft202012Validator.check_schema(op["input_schema"])
            if "sample_inputs" in op:
                jsonschema.validate(op["sample_inputs"], op["input_schema"])
    except jsonschema.ValidationError as exc:
        raise ValueError(f"{exc.json_path}: {exc.message}") from None
    except jsonschema.SchemaError as exc:
        raise ValueError(f"operation.input_schema is not a valid JSON Schema: {exc.message}") from None
    op = config.get("operation") or {}
    need = {"function": ["target"], "command": ["argv"], "asset": [], "http": ["method", "path"]}.get(op.get("kind"), [])
    missing = [k for k in need if k not in op]
    if missing:
        raise ValueError(f"operation of kind '{op['kind']}' needs {missing}")


def config_hash(config: Optional[Dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(config or {}, sort_keys=True).encode()).hexdigest()[:16]


class NeedsConfig(ImportFailure):
    """Cognex can't settle part of the plan on its own: says exactly what is missing and gives a
    repo-configuration template that fills it in (saved once, reused by every later import)."""

    def __init__(self, reason: str, missing: List[Dict[str, str]], template: Dict[str, Any],
                 code: str = "needs_configuration", **details):
        super().__init__(code, reason, missing=missing, config_template=template, **details)


class NeedsRuntime(ImportFailure):
    def __init__(self, runtime: str, reason: str, missing: List[Dict[str, str]], **details):
        super().__init__("runtime_not_available", reason, runtime=runtime, missing=missing, **details)


# -- reading files (data only) -----------------------------------------------------------------------
def _read(p: Path) -> Optional[str]:
    if p.is_file() and p.stat().st_size <= MAX_PARSE:
        return p.read_text(encoding="utf-8", errors="replace")
    return None


def _json(p: Path) -> Optional[dict]:
    text = _read(p)
    try:
        data = json.loads(text) if text else None
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _first(base: Path, *names: str) -> Optional[str]:
    return next((n for n in names if (base / n).is_file()), None)


def requirement_lines(text: str) -> List[str]:
    """Requirements from a requirements.txt: joins `\\` continuations, drops comments, options
    (--hash, -r, -e, --index-url...) and environment markers stay attached."""
    joined = re.sub(r"\\\s*\n", " ", text)
    out = []
    for line in joined.splitlines():
        line = line.split(" #")[0].strip()
        if not line or line.startswith(("#", "-")):
            continue
        line = re.split(r"\s+--?\w", line)[0].strip()       # trailing per-requirement options (--hash=...)
        if line and line[0].isalnum():
            out.append(line)
    return out


def _setup_py_scripts(text: str) -> Dict[str, str]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) == "setup":
            for kw in node.keywords:
                if kw.arg == "entry_points":
                    try:
                        eps = ast.literal_eval(kw.value)
                    except (ValueError, SyntaxError, RecursionError):
                        return {}
                    items = eps.get("console_scripts", []) if isinstance(eps, dict) else []
                    return dict(_script_entry(s) for s in items if isinstance(s, str) and "=" in s)
    return {}


def _script_entry(spec: str):
    name, _, target = spec.partition("=")
    return name.strip(), target.strip().split("[")[0].strip()


def _python(base: Path) -> Optional[Dict[str, Any]]:
    pyproject, setup_py, setup_cfg = _read(base / "pyproject.toml"), _read(base / "setup.py"), _read(base / "setup.cfg")
    if not (pyproject or setup_py or setup_cfg):
        return None
    info: Dict[str, Any] = {"build": True, "build_requires": ["setuptools>=70.1"],  # builds wheels itself
                            "backend": "setuptools.build_meta:__legacy__", "scripts": {}, "name": None}
    if pyproject:
        try:
            data = tomllib.loads(pyproject)
        except tomllib.TOMLDecodeError:
            data = {}
        bs = data.get("build-system") or {}
        if bs:
            info["build_requires"] = [r for r in bs.get("requires", []) if isinstance(r, str)]
            info["backend"] = bs.get("build-backend") or "setuptools.build_meta:__legacy__"
        proj = data.get("project") or {}
        info["requires"] = [d for d in proj.get("dependencies") or [] if isinstance(d, str)] + \
            list(((data.get("tool", {}).get("poetry", {}) or {}).get("dependencies") or {}).keys())
        info["name"] = proj.get("name") or (data.get("tool", {}).get("poetry", {}) or {}).get("name")
        info["scripts"] = {k: v for k, v in (proj.get("scripts") or {}).items() if isinstance(v, str)}
        if not info["scripts"]:
            info["scripts"] = {k: v for k, v in ((data.get("tool", {}).get("poetry", {}) or {})
                                                 .get("scripts") or {}).items() if isinstance(v, str)}
    req = _read(base / "requirements.txt")
    if req:
        info.setdefault("requires", [])
        info["requires"] += requirement_lines(req)
    if setup_py and not info["scripts"]:
        info["scripts"] = _setup_py_scripts(setup_py)
    if setup_cfg and not info["scripts"]:
        m = re.search(r"console_scripts\s*=\s*\n((?:[ \t]+\S.*\n?)+)", setup_cfg)
        if m:
            info["scripts"] = dict(_script_entry(l.strip()) for l in m.group(1).splitlines() if "=" in l)
    return info


def _node(base: Path) -> Optional[Dict[str, Any]]:
    pkg = _json(base / "package.json")
    if pkg is None:
        return None
    lock = _first(base, "package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb")
    manager = {"pnpm-lock.yaml": "pnpm", "yarn.lock": "yarn", "bun.lockb": "bun"}.get(lock or "", "npm")
    pm = str(pkg.get("packageManager") or "")
    if pm.split("@")[0] in ("pnpm", "yarn", "npm", "bun"):
        manager = pm.split("@")[0]
    bins = pkg.get("bin")
    if isinstance(bins, str):
        bins = {str(pkg.get("name", "cli")).split("/")[-1]: bins}
    scripts = pkg.get("scripts") if isinstance(pkg.get("scripts"), dict) else {}
    exports = pkg.get("exports")
    main = pkg.get("main") or pkg.get("module")
    if not main and isinstance(exports, str):
        main = exports
    if not main and isinstance(exports, dict):
        dot = exports.get(".", exports)
        while isinstance(dot, dict):  # {"import": ..., "require": ...} or {"default": ...}
            dot = dot.get("import") or dot.get("default") or dot.get("require") or dot.get("node") or \
                next(iter(dot.values()), None)
        main = dot if isinstance(dot, str) else None
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    return {"name": pkg.get("name"), "version": pkg.get("version"), "manager": manager, "lockfile": lock,
            "main": main, "bin": bins if isinstance(bins, dict) else {}, "type": pkg.get("type") or "commonjs",
            "build_script": "build" if "build" in scripts else None,
            "typescript": bool(_first(base, "tsconfig.json")) or "typescript" in deps,
            "style": pkg.get("style") or pkg.get("sass") or pkg.get("less"),
            "has_install_scripts": any(k in scripts for k in ("preinstall", "install", "postinstall")),
            "description": pkg.get("description") or "",
            "server_deps": sorted(set(pkg.get("dependencies") or {}) & SERVER_DEPS),
            "has_start": "start" in scripts,
            "creates_server": _creates_server(base, pkg, main, scripts),
            "is_library": bool(pkg.get("exports") or pkg.get("types") or pkg.get("typings") or pkg.get("module"))}


def _is_frontend(base: Path, node: Dict[str, Any]) -> bool:
    if node.get("style"):
        return True
    main = str(node.get("main") or "")
    if main.endswith((".css", ".scss", ".sass", ".less")):
        return True
    if node.get("main") or node.get("bin"):
        return False
    styles = [p for ext in ("*.css", "*.scss", "*.sass", "*.less") for p in base.rglob(ext)
              if "node_modules" not in p.parts][:200]
    scripts = [p for ext in ("*.js", "*.mjs", "*.ts") for p in base.rglob(ext)
               if "node_modules" not in p.parts and not re.search(r"(?i)(config|gulpfile|gruntfile)", p.name)][:200]
    return len(styles) > 0 and len(styles) >= len(scripts)


def _creates_server(base: Path, pkg: dict, main: Optional[str], scripts: dict) -> bool:
    """The package's main or start file starts an HTTP server itself (Node's http module)."""
    m = re.search(r"\bnode\s+([\w./-]+\.(?:c?js|mjs))", str(scripts.get("start") or ""))
    for rel in [m.group(1) if m else None, main, "server.js", "index.js", "app.js"]:
        text = _read(base / rel) if rel else None
        if text and re.search(r"\b(?:http|https)\.createServer\(|\bcreateServer\(", text):
            return True
    return False


SERVER_DEPS = {"express", "koa", "fastify", "@hapi/hapi", "hapi", "restify", "socket.io", "next", "nuxt",
               "@nestjs/core", "sails", "loopback", "@loopback/core"}


def _is_service(node: Dict[str, Any]) -> bool:
    return bool(node.get("server_deps") or node.get("creates_server")) and bool(node.get("has_start") or node.get("main"))         and not node.get("is_library") and not node.get("bin")


def _native(base: Path) -> Optional[Dict[str, Any]]:
    cmake = _read(base / "CMakeLists.txt")
    if cmake is not None:
        return {"system": "cmake", "executables_declared": re.findall(r"(?i)add_executable\s*\(\s*([A-Za-z0-9_.+-]+)", cmake)[:20],
                "fetches_at_build": bool(re.search(r"(?i)\b(FetchContent_\w+|ExternalProject_Add)\b", cmake))}
    if (base / "configure").is_file():
        return {"system": "autotools"}
    if _first(base, "configure.ac", "configure.in"):
        return {"system": "autotools", "autoreconf": True}   # configure is generated: autoreconf first
    if _first(base, "Makefile", "makefile", "GNUmakefile"):
        return {"system": "make"}
    return None


def detect(src: Path, repo_name: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The build plan for a downloaded repo: detection, then the saved repo configuration on top."""
    config = config or {}
    root = config.get("root", "").strip("/")
    base = src / root if root else src
    if not base.is_dir():
        raise NeedsConfig(f"The configured root '{root}' isn't a folder in this repository.",
                          [{"field": "root", "why": "must be a folder of the repository"}], {"root": ""})
    found = {"python": _python(base), "node": _node(base), "native": _native(base)}
    signals = sorted(n for n in ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "package.json",
                                 "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "CMakeLists.txt", "Makefile",
                                 "configure", "Dockerfile", "docker-compose.yml", "compose.yaml", "Containerfile",
                                 "pixi.toml", "environment.yml", "Cargo.toml", "go.mod", "pom.xml", "build.gradle",
                                 ".gitmodules") if (base / n).is_file())
    from app.imports import services
    py_apps = services.detect_python(base) if not config.get("runtime") else []
    node_service = bool(found["node"] and _is_service(found["node"]))
    services_found = bool(py_apps) or node_service
    docker = [n for n in ("Dockerfile", "Containerfile", "docker-compose.yml", "docker-compose.yaml", "compose.yaml",
                          "compose.yml") if (base / n).is_file()]
    conda = [n for n in ("pixi.toml", "pixi.lock", "environment.yml", "environment.yaml", "meta.yaml")
             if (base / n).is_file()]
    runtime = config.get("runtime")
    if runtime is None:
        if services_found:
            runtime = "service"
        elif found["python"]:
            runtime = "python"
        elif found["node"]:
            runtime = "frontend" if _is_frontend(base, found["node"]) else "node"
        elif found["native"]:
            runtime = "native"
        elif "pixi.toml" in conda:
            runtime = "conda"          # e.g. a pixi/conda-managed application pipeline
        elif docker:
            runtime = "container"
        elif any(p.suffix == ".py" for p in base.glob("*.py")) or any(base.glob("*/__init__.py")):
            runtime = "python"         # a plain source tree (no packaging metadata)
            found["python"] = {"build": False, "build_requires": [], "scripts": {}, "name": None}
        else:
            other = [s for s in signals if s in ("Cargo.toml", "go.mod", "pom.xml", "build.gradle")]
            raise NeedsRuntime("unknown", "Cognex couldn't tell how this repository is built"
                               + (f" (it has {', '.join(other)}, an ecosystem Cognex has no runtime for yet)." if other
                                  else ": no Python, JavaScript, C/C++ or container project files were found."),
                               [{"field": "runtime", "why": "no supported project files ("
                                 + ", ".join(signals or ["none found"]) + ")"}], signals=signals)
    plan: Dict[str, Any] = {"runtime": runtime, "root": root, "signals": signals, "docker": docker, "conda": conda}
    if runtime == "container" and found["node"]:
        plan["node"] = found["node"]
    if runtime == "service":
        apps = py_apps or ([] if found["node"] else services.detect_python(base))
        if apps:
            plan["service"] = {"runtime": "python", "apps": apps[:10], "app": apps[0]}
            plan["python"] = found["python"] or {"build": False, "build_requires": [], "scripts": {}, "name": None}
        elif found["node"]:
            plan["service"] = {"runtime": "node"}
            plan["node"] = found["node"]
        elif not (config.get("service") or {}).get("start"):
            raise NeedsConfig("The configuration says runtime 'service', but Cognex found no web app to start.",
                              [{"field": "service.start", "why": "the command that starts the server on $PORT"}],
                              {"service": {"start": ["python", "-m", "uvicorn", "module:app", "--port", "{port}"]}})
        else:
            plan["service"] = {"runtime": "python" if found["python"] else "node"}
    if runtime == "python":
        plan["python"] = found["python"] or {"build": False, "build_requires": [], "scripts": {}, "name": None}
    elif runtime in ("node", "frontend"):
        if not found["node"]:
            raise NeedsConfig(f"The configuration says runtime '{runtime}', but there is no package.json in "
                              f"'{root or '/'}'.", [{"field": "root", "why": "the folder that holds package.json"}],
                              {"root": "path/to/package"})
        plan["node"] = found["node"]
    elif runtime == "native":
        native = found["native"] or {}
        if not native and not (config.get("build") or {}).get("commands"):
            raise NeedsConfig("The configuration says runtime 'native', but there is no CMakeLists.txt, configure "
                              "script or Makefile here.", [{"field": "build.commands", "why": "how to build it"}],
                              {"build": {"commands": [["make"]]}, "executables": ["path/to/binary"]})
        plan["native"] = native
    plan["config"] = copy.deepcopy(config)
    for key in ("package", "entry_point", "version", "extra_requirements", "build", "executables", "assets",
                "operation", "profile"):
        if key in config:
            plan[key] = copy.deepcopy(config[key])
    return plan


def unavailable(plan: Dict[str, Any]) -> NeedsRuntime:
    """Why a container / conda project can't run here yet, and what it would take."""
    if plan["runtime"] == "conda":
        return NeedsRuntime(
            "conda", "This project's environment is managed by conda/pixi (" + ", ".join(plan["conda"]) + "): its "
            "dependencies are conda packages, often native toolchains, not pip or npm packages. Cognex has no "
            "conda runtime yet" + (", and the project also ships a Dockerfile, so it is normally run as a container"
                                   if plan["docker"] else "") + ".",
            [{"field": "runtime", "why": "a conda/pixi runtime (environment solver + package cache) on the build "
                                         "workers"}] + ([{"field": "runtime", "why": "or a container runtime to run "
                                                          "its image (" + ", ".join(plan["docker"]) + ")"}]
                                                        if plan["docker"] else []),
            signals=plan["signals"])
    service = (plan.get("node") or {}).get("server_deps")
    return NeedsRuntime(
        "container", ("This project is a network service (" + ", ".join(service) + ") shipped as a container ("
                      if service else "This project is distributed as a container (") + ", ".join(plan["docker"]) +
        "), not as a library or command-line tool Cognex can build. Running container images needs a container "
        "runtime (Docker/OCI) on the build workers, which this server doesn't have.",
        [{"field": "runtime", "why": "a container runtime (Docker/OCI) for isolated build workers"}],
        signals=plan["signals"])
