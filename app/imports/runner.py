"""Runs INSIDE the sandbox (see sandbox_boot.py), after lockdown, next to untrusted code.

Modes:
- discover: import the package, find its main callable, learn real sample calls from the README
  and docstrings (by running those examples with the candidate functions instrumented), infer an
  input JSON Schema plus converters, and test the function through the same path production uses.
- call:     run one imported operation on JSON inputs (production execution path).
- selftest: report what the sandbox allows (network, secrets, files, identity, limits).

Everything this module returns is treated as untrusted by the caller (validated and size-limited).
"""
from __future__ import annotations

import ast
import dataclasses
import doctest
import enum
import importlib
import inspect
import json
import math
import os
import re
import sys
import types
import typing
from typing import Any, Dict, List, Optional, Tuple

IO_WORDS = {"read", "write", "load", "save", "open", "download", "upload", "fetch", "print", "plot", "show", "main",
            "cli", "serve", "server", "connect", "setup", "install", "configure", "log", "debug", "test", "demo",
            "register", "reset", "set", "get_logger", "run_server", "draw", "display", "render"}
MAX_CALLS_RECORDED = 6
JSON_SCALARS = (str, int, float, bool, type(None))


class InputError(Exception):
    pass


class OutputError(Exception):
    pass


# -- JSON conversion ---------------------------------------------------------------------------------
def to_json(value: Any, depth: int = 0) -> Any:
    if depth > 20:
        raise OutputError("the result is nested too deeply")
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, enum.Enum):
        return to_json(value.value, depth + 1)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise OutputError(f"the result contains a non-finite number ({value})")
        return value
    if isinstance(value, complex):
        return {"real": value.real, "imag": value.imag}
    mod = type(value).__module__
    if mod.startswith("numpy"):
        if hasattr(value, "tolist"):
            return to_json(value.tolist(), depth + 1)
    if isinstance(value, dict):
        return {str(to_json(k, depth + 1)): to_json(v, depth + 1) for k, v in value.items()}
    if hasattr(value, "_asdict"):
        return to_json(value._asdict(), depth + 1)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return to_json(dataclasses.asdict(value), depth + 1)
    if isinstance(value, (list, tuple, set, frozenset, range)):
        items = list(value)
        if isinstance(value, (set, frozenset)):
            try:
                items = sorted(items)
            except TypeError:
                pass
        return [to_json(v, depth + 1) for v in items]
    if mod.startswith("decimal") or mod.startswith("fractions"):
        return float(value)
    if mod == "datetime":
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        import base64
        return {"base64": base64.b64encode(bytes(value)).decode()}
    # common library objects: GeoJSON-like geometries, arrays, data frames / series, paths
    if hasattr(value, "__geo_interface__"):
        return to_json(value.__geo_interface__, depth + 1)
    if mod.startswith("pandas") and hasattr(value, "to_dict"):
        try:
            return to_json(value.to_dict(orient="records") if hasattr(value, "columns") else value.tolist(), depth + 1)
        except TypeError:
            return to_json(value.to_dict(), depth + 1)
    if hasattr(value, "tolist") and callable(value.tolist):
        return to_json(value.tolist(), depth + 1)
    if hasattr(value, "isoformat") and callable(value.isoformat):
        return value.isoformat()
    if mod.startswith("pathlib"):
        return str(value)
    raise OutputError(f"the result is a {type(value).__name__}, which can't be converted to JSON")


# -- types -> JSON Schema + converters ------------------------------------------------------------------
def _qual(obj) -> str:
    return f"{obj.__module__}:{obj.__qualname__}"


def schema_for_type(tp: Any) -> Tuple[Optional[dict], Optional[str]]:
    """(JSON schema, converter) for a type annotation; (None, None) if unsupported."""
    if tp is inspect.Parameter.empty or tp is typing.Any:
        return None, None
    origin, args = typing.get_origin(tp), typing.get_args(tp)
    if tp in (str,):
        return {"type": "string"}, None
    if tp is bool:
        return {"type": "boolean"}, None
    if tp is int:
        return {"type": "integer"}, None
    if tp is float:
        return {"type": "number"}, None
    if tp is type(None):
        return {"type": "null"}, None
    if isinstance(tp, type) and issubclass(tp, enum.Enum):
        values = [m.value for m in tp]
        if all(isinstance(v, (str, int, float)) and not isinstance(v, bool) for v in values):
            return {"enum": values}, "enum:" + _qual(tp)
        return {"enum": [m.name for m in tp]}, "enumname:" + _qual(tp)
    if origin is typing.Literal:
        return {"enum": list(args)}, None
    if origin in (typing.Union, getattr(types, "UnionType", ())):
        subs = [schema_for_type(a) for a in args]
        if any(s is None for s, _ in subs):
            return None, None
        convs = {c for _, c in subs if c}
        return {"anyOf": [s for s, _ in subs]}, (convs.pop() if len(convs) == 1 else None)
    if origin in (list, typing.List, set, frozenset, tuple) or tp in (list, tuple, set):
        conv = {tuple: "tuple", set: "set", frozenset: "set"}.get(origin or tp)
        if origin is tuple and args and args[-1] is not Ellipsis:
            subs = [schema_for_type(a)[0] for a in args]
            if any(s is None for s in subs):
                return None, None
            return {"type": "array", "prefixItems": subs, "minItems": len(args), "maxItems": len(args)}, "tuple"
        item = schema_for_type(args[0])[0] if args else {}
        if item is None:
            return None, None
        return {"type": "array", "items": item}, conv
    if origin in (dict, typing.Dict) or tp is dict:
        val = schema_for_type(args[1])[0] if len(args) == 2 else {}
        if val is None:
            return None, None
        return {"type": "object", "additionalProperties": val}, None
    import collections.abc as cabc
    if origin in (cabc.Sequence, cabc.Iterable, cabc.Collection, cabc.MutableSequence):
        item = schema_for_type(args[0])[0] if args else {}
        return ({"type": "array", "items": item}, None) if item is not None else (None, None)
    name = getattr(tp, "__name__", str(tp))
    if name == "ndarray" or "ndarray" in str(tp) or "ArrayLike" in str(tp):
        return {"type": "array"}, "ndarray"
    return None, None


def schema_for_value(v: Any) -> Tuple[Optional[dict], Optional[str]]:
    """Schema + converter inferred from an observed example value."""
    if isinstance(v, enum.Enum):
        cls = type(v)
        values = [m.value for m in cls]
        if all(isinstance(x, (str, int, float)) and not isinstance(x, bool) for x in values):
            return {"enum": values}, "enum:" + _qual(cls)
        return {"enum": [m.name for m in cls]}, "enumname:" + _qual(cls)
    if isinstance(v, bool):
        return {"type": "boolean"}, None
    if isinstance(v, int):
        return {"type": "integer"}, None
    if isinstance(v, float):
        return {"type": "number"}, None
    if isinstance(v, str):
        return {"type": "string"}, None
    if v is None:
        return None, None
    if type(v).__module__.startswith("numpy") and hasattr(v, "tolist"):
        return {"type": "array"}, "ndarray"
    if isinstance(v, (list, tuple)):
        conv = "tuple" if isinstance(v, tuple) else None
        if v and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in v):
            s = {"type": "array", "items": {"type": "number"}}
            if isinstance(v, tuple):
                s.update(minItems=len(v), maxItems=len(v))
            return s, conv
        if v and all(isinstance(x, str) for x in v):
            return {"type": "array", "items": {"type": "string"}}, conv
        if v and all(isinstance(x, (list, tuple)) for x in v):
            return {"type": "array", "items": {"type": "array"}}, "tuple" if all(isinstance(x, tuple) for x in v) and conv else conv
        return {"type": "array"}, conv
    if isinstance(v, dict) and all(isinstance(k, str) for k in v):
        return {"type": "object"}, None
    return None, None


def convert_in(value: Any, conv: Optional[str]) -> Any:
    if conv is None or value is None:
        return value
    if conv == "tuple":
        if not isinstance(value, list):
            raise InputError("expected a list")
        return tuple(tuple(x) if isinstance(x, list) else x for x in value) if value and all(
            isinstance(x, list) for x in value) else tuple(value)
    if conv == "set":
        return set(value)
    if conv == "ndarray":
        import numpy
        return numpy.asarray(value)
    if conv.startswith(("enum:", "enumname:")):
        kind, _, path = conv.partition(":")
        mod, _, qual = path.partition(":")
        cls = importlib.import_module(mod)
        for part in qual.split("."):
            cls = getattr(cls, part)
        try:
            return cls(value) if kind == "enum" else cls[value]
        except (ValueError, KeyError):
            raise InputError(f"must be one of {[m.value if kind == 'enum' else m.name for m in cls]}") from None
    return value


# -- discovery -----------------------------------------------------------------------------------------
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _first_line(doc: Optional[str]) -> str:
    for line in (doc or "").strip().splitlines():
        s = line.strip()
        if s:
            return s[:300]
    return ""


def _candidates(module, pkg: str) -> Dict[str, Any]:
    names = getattr(module, "__all__", None)
    if not isinstance(names, (list, tuple)):
        names = [n for n in dir(module) if not n.startswith("_")]
    out = {}
    for n in names:
        if not isinstance(n, str) or n.startswith("_"):
            continue
        obj = getattr(module, n, None)
        if inspect.isfunction(obj) and (getattr(obj, "__module__", "") or "").split(".")[0] == pkg:
            out[n] = obj
    if not out:  # nothing at the top level: public functions of already-imported submodules
        for modname, mod in list(sys.modules.items()):
            if modname.startswith(pkg + ".") and not any(p.startswith("_") for p in modname.split(".")[1:]):
                for n, obj in vars(mod).items():
                    if not n.startswith("_") and inspect.isfunction(obj) and obj.__module__ == modname:
                        out.setdefault(n, obj)
    return out


class Recorder:
    def __init__(self):
        self.calls: Dict[str, List[dict]] = {}
        self.current_expect: Optional[str] = None

    def wrap(self, name: str, fn):
        rec = self

        def wrapper(*args, **kwargs):
            entry = {"args": args, "kwargs": kwargs, "expect": rec.current_expect}
            try:
                result = fn(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 - record and re-raise
                entry["error"] = f"{type(exc).__name__}: {exc}"[:300]
                if len(rec.calls.setdefault(name, [])) < MAX_CALLS_RECORDED:
                    rec.calls[name].append(entry)
                raise
            entry["result"] = result
            if len(rec.calls.setdefault(name, [])) < MAX_CALLS_RECORDED:
                rec.calls[name].append(entry)
            return result

        wrapper.__wrapped__ = fn
        wrapper.__name__ = getattr(fn, "__name__", name)
        wrapper.__doc__ = fn.__doc__
        return wrapper


def _code_blocks(readme: str) -> List[str]:
    blocks = re.findall(r"```(?:python|py|pycon|python3)?[^\n]*\n(.*?)```", readme, flags=re.S)
    blocks += re.findall(r"::\n\n((?:[ \t]+.*\n|\n)+)", readme)  # reStructuredText literal blocks
    blocks += re.findall(r"(?m)^\.\. (?:code|code-block|sourcecode|doctest)::[^\n]*\n(?:[ \t]+:\w[\w-]*:.*\n)*\n"
                         r"((?:[ \t]+.*\n|[ \t]*\n)+)", readme)  # reStructuredText code directives
    return [b for b in blocks if "(" in b][:40]


def _statements(block: str) -> List[Tuple[str, Optional[str]]]:
    """(source, expected output) pairs from a README block; accepts '>>> ' doctest style and the
    common '>> value' / '# => value' output notes."""
    import textwrap
    block = textwrap.dedent(block)
    if ">>>" in block:
        try:
            return [(ex.source, ex.want.strip() or None) for ex in doctest.DocTestParser().get_examples(block)]
        except ValueError:
            return []
    out: List[Tuple[str, Optional[str]]] = []
    pending: List[str] = []
    for line in block.splitlines():
        m = re.match(r"^\s*(?:>>|#\s*=>|#\s*->|# Output:|# returns)\s*(.+)$", line)
        if m and out:
            src, _ = out[-1]
            out[-1] = (src, m.group(1).split("#")[0].strip())
            continue
        pending.append(line)
        src = "\n".join(pending)
        try:
            ast.parse(src)
        except SyntaxError:
            continue
        if src.strip():
            out.append((src, None))
        pending = []
    return out


def _run_examples(snippets: List[Tuple[str, Optional[str]]], ns: dict, rec: Recorder) -> None:
    for src, expect in snippets[:200]:
        rec.current_expect = expect
        try:
            code = compile(src, "<example>", "exec")
            exec(code, ns)  # noqa: S102 - untrusted README code, inside the sandbox by design
        except BaseException:  # noqa: BLE001
            pass
        finally:
            rec.current_expect = None


def _parse_expect(text: Optional[str]):
    if not text:
        return None, False
    try:
        return ast.literal_eval(text.strip()), True
    except (ValueError, SyntaxError, RecursionError):
        return None, False


def _close(a, b) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-9)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_close(x, y) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_close(a[k], b[k]) for k in a)
    return a == b


def _synth(schema: dict) -> Any:
    if "enum" in schema:
        return schema["enum"][0]
    if "anyOf" in schema:
        for s in schema["anyOf"]:
            if s.get("type") != "null":
                return _synth(s)
    t = schema.get("type")
    if t == "string":
        return "Cognex example"
    if t == "integer":
        return 3
    if t == "number":
        return 1.5
    if t == "boolean":
        return False
    if t == "array":
        if "prefixItems" in schema:
            return [_synth(s) for s in schema["prefixItems"]]
        item = schema.get("items") or {"type": "number"}
        return [_synth(item) for _ in range(3)]
    if t == "object":
        return {}
    return None


def _plan_params(fn, observed: Optional[dict]) -> Tuple[Optional[list], Optional[str]]:
    """Per-parameter plan [{name, schema, convert, required, kind, default}] or (None, reason)."""
    sig = inspect.signature(fn)
    try:
        hints = typing.get_type_hints(fn)
    except Exception:  # noqa: BLE001 - unresolvable forward references etc.
        hints = {}
    plan = []
    for p in sig.parameters.values():
        if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        required = p.default is p.empty
        schema = conv = None
        if p.name in hints:
            schema, conv = schema_for_type(hints[p.name])
        if schema is None and not required:
            schema, conv = schema_for_value(p.default)
        if schema is None and observed and p.name in observed:
            schema, conv = schema_for_value(observed[p.name])
        if schema is None:
            if required:
                return None, f"parameter '{p.name}' has no type information and no documented example value"
            continue  # optional parameter Cognex can't express: left at its default
        entry = {"name": p.name, "schema": schema, "convert": conv, "required": required,
                 "positional_only": p.kind == p.POSITIONAL_ONLY}
        if not required:
            try:
                entry["schema"] = {**schema, "default": to_json(p.default)}
            except OutputError:
                pass
        plan.append(entry)
    return plan, None


def _bind_observed(fn, call: dict) -> Optional[dict]:
    try:
        bound = inspect.signature(fn).bind(*call["args"], **call["kwargs"])
    except TypeError:
        return None
    return dict(bound.arguments)


def call_with_plan(fn, params: list, inputs: dict) -> Any:
    args, kwargs = [], {}
    for p in params:
        if p["name"] not in inputs:
            if p["required"]:
                raise InputError(f"missing required input '{p['name']}'")
            continue
        try:
            value = convert_in(inputs[p["name"]], p.get("convert"))
        except InputError as exc:
            raise InputError(f"input '{p['name']}': {exc}") from None
        except (TypeError, ValueError) as exc:
            raise InputError(f"input '{p['name']}': {exc}") from None
        if p.get("positional_only"):
            args.append(value)
        else:
            kwargs[p["name"]] = value
    return fn(*args, **kwargs)


def _resolve(module_name: str, function: str):
    module = importlib.import_module(module_name)
    obj = module
    for part in function.split("."):
        obj = getattr(obj, part)
    return obj


def discover(payload: dict) -> dict:
    pkg, repo_name = payload["module"], payload.get("repo_name", "")
    try:
        module = importlib.import_module(pkg)
    except ModuleNotFoundError as exc:
        missing = (exc.name or "").split(".")[0]
        if missing and missing != pkg:
            return {"ok": False, "code": "unsupported_dependency",
                    "reason": f"Importing the package needs '{missing}', which isn't a declared dependency or "
                              f"has no prebuilt package."}
        return {"ok": False, "code": "import_failed", "reason": f"The package couldn't be imported: {exc}"[:500]}
    except BaseException as exc:  # noqa: BLE001
        return {"ok": False, "code": "import_failed",
                "reason": f"Importing the package failed: {type(exc).__name__}: {exc}"[:500]}

    cands = _candidates(module, pkg)
    if not cands:
        return {"ok": False, "code": "no_callable_function",
                "reason": "The package exposes no public functions to call (classes and command-line tools "
                          "aren't supported yet)."}
    rec = Recorder()
    originals = {}
    for name, fn in cands.items():
        originals[name] = fn
        wrapped = rec.wrap(name, fn)
        for mod in [module] + [m for n, m in list(sys.modules.items()) if n.startswith(pkg + ".")]:
            if getattr(mod, name, None) is fn:
                setattr(mod, name, wrapped)

    readme = payload.get("readme") or ""
    snippets: List[Tuple[str, Optional[str]]] = []
    for name, fn in originals.items():  # docstring examples: doctests and ``inline(calls)``
        doc = fn.__doc__ or ""
        try:
            snippets += [(ex.source, ex.want.strip() or None) for ex in doctest.DocTestParser().get_examples(doc)]
        except ValueError:
            pass
        snippets += [(m, None) for m in re.findall(rf"``({re.escape(name)}\(.*?\))``", doc)]
    for block in _code_blocks(readme):
        snippets += _statements(block)
    ns: Dict[str, Any] = {"__name__": "__cognex_examples__", pkg: module}
    ns.update({k: getattr(module, k) for k in dir(module) if not k.startswith("_")})
    _run_examples(snippets, ns, rec)
    for name, fn in originals.items():  # restore
        for mod in [module] + [m for n, m in list(sys.modules.items()) if n.startswith(pkg + ".")]:
            if getattr(mod, name, None) is not None and getattr(getattr(mod, name), "__wrapped__", None) is fn:
                setattr(mod, name, fn)

    # Score the candidates.
    keys = {_norm(pkg), _norm(repo_name), _norm(repo_name.removeprefix("python-").removeprefix("py"))}
    scored = []
    for name, fn in originals.items():
        ok_calls = [c for c in rec.calls.get(name, []) if "error" not in c]
        score = 0.0
        score += 5 if _norm(name) in keys else 0
        score += 3 if ok_calls else 0
        score += 1 if fn.__doc__ else 0
        score += 1 if re.search(rf"\b{re.escape(name)}\(", readme) else 0
        words = set(re.split(r"_+", name.lower()))
        score -= 4 if words & IO_WORDS else 0
        observed = _bind_observed(fn, ok_calls[0]) if ok_calls else None
        params, why = _plan_params(fn, observed)
        scored.append({"name": name, "fn": fn, "score": score, "calls": ok_calls, "params": params, "why": why,
                       "observed": observed})
    scored.sort(key=lambda c: -c["score"])
    entry = payload.get("entry_point")
    if entry:
        chosen = next((c for c in scored if c["name"] == entry), None)
        if chosen is None:
            return {"ok": False, "code": "entry_point_not_found",
                    "reason": f"'{entry}' isn't a public function of the package. Public functions: "
                              + ", ".join(c["name"] for c in scored[:12])}
    else:
        usable = [c for c in scored if c["params"] is not None]
        if not usable:
            reasons = "; ".join(f"{c['name']}: {c['why']}" for c in scored[:5])
            return {"ok": False, "code": "unsupported_input",
                    "reason": f"No public function has inputs Cognex can express as JSON ({reasons})."[:900]}
        top = usable[0]
        rivals = [c for c in usable[1:] if c["score"] >= top["score"] - 1]
        if rivals or top["score"] < 2:
            names = [top["name"]] + [c["name"] for c in rivals]
            return {"ok": False, "code": "multiple_entry_points",
                    "reason": ("Several functions look like the main algorithm: " if rivals else
                               "Cognex couldn't tell which function is the main algorithm. Candidates: ")
                    + ", ".join(f"{n}()" for n in names[:6])
                    + ". Resubmit with entry_point set to the one you want.",
                    "candidates": names[:10]}
        chosen = top
    if chosen["params"] is None:
        return {"ok": False, "code": "unsupported_input", "reason": f"{chosen['name']}(): {chosen['why']}."}

    fn, params = chosen["fn"], chosen["params"]
    properties = {p["name"]: p["schema"] for p in params}
    schema = {"type": "object", "properties": properties, "required": [p["name"] for p in params if p["required"]]}
    expected, has_expected = None, False
    if chosen["observed"] is not None:
        call = chosen["calls"][0]
        sample = {}
        for p in params:
            if p["name"] in chosen["observed"]:
                try:
                    sample[p["name"]] = to_json(chosen["observed"][p["name"]])
                except OutputError:
                    pass
        expected, has_expected = _parse_expect(call.get("expect"))
        if not has_expected:
            try:
                expected, has_expected = to_json(call["result"]), True  # what the documented call returned
            except OutputError:
                pass
        source = "documented example"
    else:
        sample = {p["name"]: _synth(p["schema"]) for p in params if p["required"]}
        source = "generated from parameter types"

    # Test through the production path: JSON inputs -> converters -> function -> JSON output.
    try:
        import jsonschema
        jsonschema.validate(sample, schema)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "code": "test_failed",
                "reason": f"The sample inputs don't match the inferred schema: {str(exc)[:300]}"}
    try:
        result = call_with_plan(fn, params, sample)
    except InputError as exc:
        return {"ok": False, "code": "test_failed", "reason": f"Test call of {chosen['name']}() rejected the sample "
                                                              f"inputs: {exc}"[:600]}
    except BaseException as exc:  # noqa: BLE001
        return {"ok": False, "code": "test_failed",
                "reason": f"Test call {chosen['name']}({json.dumps(sample)[:200]}) raised "
                          f"{type(exc).__name__}: {str(exc)[:300]}"}
    if result is None:
        return {"ok": False, "code": "unsupported_output",
                "reason": f"{chosen['name']}() returned nothing, so there is no result to give back over the API."}
    try:
        output = to_json(result)
    except OutputError as exc:
        return {"ok": False, "code": "unsupported_output", "reason": f"{chosen['name']}(): {exc}."}
    if has_expected and not _close(output, expected):
        return {"ok": False, "code": "test_failed",
                "reason": f"Test call returned {json.dumps(output)[:200]} but the documentation says "
                          f"{json.dumps(expected)[:200]}."}
    return {
        "ok": True, "function": chosen["name"], "module": pkg,
        "signature": f"{chosen['name']}{inspect.signature(fn)}"[:300],
        "summary": _first_line(fn.__doc__), "params": params, "input_schema": schema,
        "sample_inputs": sample, "sample_output": {"result": output}, "sample_source": source,
        "expected_checked": has_expected,
        "candidates": [{"name": c["name"], "score": c["score"]} for c in scored[:10]],
    }


def call(payload: dict) -> dict:
    spec = payload["spec"]
    try:
        fn = _resolve(spec["module"], spec["function"])
    except BaseException as exc:  # noqa: BLE001
        return {"ok": False, "error_type": "operation_failed",
                "error": f"the imported code couldn't be loaded: {type(exc).__name__}: {exc}"[:500]}
    try:
        result = call_with_plan(fn, spec["params"], payload["inputs"])
    except InputError as exc:
        return {"ok": False, "error_type": "invalid_input", "error": str(exc)[:500]}
    except MemoryError:
        return {"ok": False, "error_type": "operation_failed", "error": "the operation ran out of memory"}
    except BaseException as exc:  # noqa: BLE001
        return {"ok": False, "error_type": "operation_failed", "error": f"{type(exc).__name__}: {exc}"[:500]}
    try:
        return {"ok": True, "output": {"result": to_json(result)}}
    except OutputError as exc:
        return {"ok": False, "error_type": "operation_failed", "error": str(exc)}


def selftest(payload: dict) -> dict:
    import socket
    out: Dict[str, Any] = {"uid": os.getuid() if hasattr(os, "getuid") else None}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(3)
        s.connect(("1.1.1.1", 443))
        out["network_inet"] = "ALLOWED"
        s.close()
    except OSError as exc:
        out["network_inet"] = f"blocked ({type(exc).__name__}: {exc.errno})"
    try:
        socket.getaddrinfo("github.com", 443)
        out["dns"] = "ALLOWED"
    except OSError as exc:
        out["dns"] = f"blocked ({type(exc).__name__})"
    out["env_keys"] = sorted(os.environ)
    try:
        with open("/proc/1/environ", "rb") as fh:
            fh.read(10)
        out["read_pid1_environ"] = "ALLOWED"
    except OSError as exc:
        out["read_pid1_environ"] = f"blocked ({type(exc).__name__})"
    try:
        with open(os.path.join(payload.get("app_dir", "/app"), ".cognex_write_probe"), "w") as fh:
            fh.write("x")
        out["write_app_dir"] = "ALLOWED"
    except OSError as exc:
        out["write_app_dir"] = f"blocked ({type(exc).__name__})"
    try:
        _block = bytearray(payload.get("alloc_bytes", 3 * 1024 ** 3))
        out["alloc_over_limit"] = "ALLOWED"
        del _block
    except MemoryError:
        out["alloc_over_limit"] = "blocked (MemoryError)"
    import shutil
    import subprocess
    runtimes = {}
    for name, argv in (("python", [sys.executable, "--version"]), ("node", [payload.get("node") or "node", "-e",
                       "console.log(process.version)"]), ("cc", ["cc", "--version"]), ("c++", ["c++", "--version"]),
                       ("cmake", ["cmake", "--version"]), ("make", ["make", "--version"])):
        exe = argv[0] if os.path.isabs(argv[0]) else shutil.which(argv[0])
        if not exe:
            runtimes[name] = "missing"
            continue
        try:
            proc = subprocess.run([exe, *argv[1:]], capture_output=True, timeout=30)
            text = (proc.stdout or proc.stderr).decode("utf-8", "replace").strip().splitlines()
            runtimes[name] = (text[0][:80] if text else "") if proc.returncode == 0 else f"exit {proc.returncode}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            runtimes[name] = f"failed ({type(exc).__name__})"
    out["runtimes"] = runtimes
    try:
        import resource
        out["limits"] = {n: resource.getrlimit(getattr(resource, n))
                         for n in ("RLIMIT_AS", "RLIMIT_CPU", "RLIMIT_NPROC", "RLIMIT_FSIZE", "RLIMIT_NOFILE")}
    except ImportError:
        out["limits"] = None
    return {"ok": True, "probe": out}


# -- configured function operations (a repo configuration, not an adapter) ------------------------------
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def fill(template: Any, inputs: dict) -> Any:
    """Replace "{name}" placeholders with input values: a whole-string "{x}" becomes the value itself
    (any JSON type); "{x}" inside a longer string is interpolated as text. Missing optional inputs
    drop the argument (returns _MISSING)."""
    if isinstance(template, str):
        m = _PLACEHOLDER.fullmatch(template)
        if m:
            return inputs.get(m.group(1), _MISSING)
        return _PLACEHOLDER.sub(lambda mm: str(inputs.get(mm.group(1), "")), template)
    if isinstance(template, list):
        return [v for v in (fill(t, inputs) for t in template) if v is not _MISSING]
    if isinstance(template, dict):
        return {k: v for k, v in ((k, fill(t, inputs)) for k, t in template.items()) if v is not _MISSING}
    return template


_MISSING = object()


def _target(spec: str):
    mod, _, attr = spec.partition(":")
    obj = importlib.import_module(mod)
    for part in [p for p in attr.split(".") if p]:
        obj = getattr(obj, part)
    return obj


def run_chain(spec: dict, inputs: dict) -> Any:
    fn = _target(spec["target"])
    value = fn(*fill(spec.get("positional", []), inputs), **fill(spec.get("args", {}), inputs))
    for step in spec.get("then", []):
        if "attr" in step:
            value = getattr(value, step["attr"])
        if "call" in step:
            value = getattr(value, step["call"])(*fill(step.get("positional", []), inputs),
                                                 **fill(step.get("args", {}), inputs))
    return value


def chain(payload: dict) -> dict:
    """Run (and, with expected, check) a configured Python function operation."""
    spec, inputs = payload["spec"], payload["inputs"]
    try:
        result = run_chain(spec, inputs)
    except InputError as exc:
        return {"ok": False, "error_type": "invalid_input", "code": "test_failed", "error": str(exc)[:500],
                "reason": str(exc)[:500]}
    except MemoryError:
        return {"ok": False, "error_type": "operation_failed", "code": "test_failed",
                "error": "the operation ran out of memory", "reason": "the operation ran out of memory"}
    except BaseException as exc:  # noqa: BLE001
        msg = f"{type(exc).__name__}: {exc}"[:500]
        return {"ok": False, "error_type": "operation_failed", "code": "test_failed", "error": msg, "reason": msg}
    if result is None:
        return {"ok": False, "error_type": "operation_failed", "code": "unsupported_output",
                "error": "the operation returned nothing", "reason": "the operation returned nothing"}
    try:
        return {"ok": True, "output": {"result": to_json(result)}}
    except OutputError as exc:
        return {"ok": False, "error_type": "operation_failed", "code": "unsupported_output", "error": str(exc),
                "reason": str(exc)}


# -- build commands --------------------------------------------------------------------------------------
def _tail(path: str, n: int = 4000) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - n))
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def exec_steps(payload: dict) -> dict:
    """Run build / fetch commands in order (argv lists, no shell unless the command is a shell)."""
    import subprocess
    import time
    log = os.path.join(payload["scratch"], "build.log")
    env = {**os.environ, **{k: str(v) for k, v in (payload.get("env") or {}).items()}}
    env["HOME"] = env["TMPDIR"] = payload["scratch"]
    deadline = time.monotonic() + float(payload.get("timeout", 1200))
    steps = []
    with open(log, "ab") as fh:
        for step in payload["steps"]:
            argv, cwd = [str(a) for a in step["argv"]], step.get("cwd") or payload["scratch"]
            fh.write(("\n$ " + " ".join(argv) + "\n").encode())
            fh.flush()
            started = time.monotonic()
            try:
                proc = subprocess.run(argv, cwd=cwd, env={**env, **(step.get("env") or {})}, stdout=fh,
                                      stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                      timeout=max(1.0, deadline - time.monotonic()))
                code = proc.returncode
            except FileNotFoundError:
                code = 127
                fh.write(f"command not found: {argv[0]}\n".encode())
            except subprocess.TimeoutExpired:
                code = -9
                fh.write(b"\n[timed out]\n")
            fh.flush()
            steps.append({"argv": argv[:12], "exit_code": code, "seconds": round(time.monotonic() - started, 1)})
            if code != 0 and not step.get("optional"):
                return {"ok": False, "steps": steps, "failed": argv[:12], "exit_code": code, "log_tail": _tail(log)}
    return {"ok": True, "steps": steps, "log_tail": _tail(log, 1500)}


# -- command-line operations ----------------------------------------------------------------------------
_CLI_PLACEHOLDER = re.compile(r"\{(bin|file):([A-Za-z0-9_.+-]+)\}|\{([A-Za-z_][A-Za-z0-9_]*)(\.\.\.)?\}|\{outdir\}")


def _expand_argv(template: List[str], inputs: dict, bins: dict, scratch: str) -> List[str]:
    argv: List[str] = []
    for item in template:
        m = _CLI_PLACEHOLDER.fullmatch(item)
        if m and m.group(1) == "bin":
            if m.group(2) not in bins:
                raise InputError(f"unknown executable '{m.group(2)}'")
            argv += list(bins[m.group(2)])
        elif m and m.group(1) == "file":
            value = inputs.get(m.group(2))
            if value is None:
                continue
            path = os.path.join(scratch, "in", re.sub(r"[^A-Za-z0-9_.-]", "_", m.group(2)))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as fh:
                if isinstance(value, dict) and "base64" in value:
                    import base64
                    fh.write(base64.b64decode(value["base64"]))
                else:
                    fh.write(str(value).encode("utf-8"))
            argv.append(path)
        elif m and m.group(3):
            value = inputs.get(m.group(3))
            if value is None:
                continue
            if m.group(4):  # {name...}: a list expands to several arguments
                if not isinstance(value, list):
                    raise InputError(f"input '{m.group(3)}' must be a list of strings")
                argv += [str(v) for v in value]
            else:
                argv.append(value if isinstance(value, str) else json.dumps(value))
        else:
            argv.append(item.replace("{outdir}", os.path.join(scratch, "out")))
    return argv


def command(payload: dict) -> dict:
    """Run a command-line operation: argv template + stdin, output from stdout or a file."""
    import subprocess
    spec, inputs, scratch = payload["spec"], payload["inputs"], payload["scratch"]
    os.makedirs(os.path.join(scratch, "out"), exist_ok=True)
    try:
        argv = _expand_argv(spec["argv"], inputs, spec.get("bins", {}), scratch)
    except InputError as exc:
        return {"ok": False, "error_type": "invalid_input", "error": str(exc), "code": "test_failed",
                "reason": str(exc)}
    stdin = fill(spec.get("stdin", ""), inputs) if spec.get("stdin") else None
    if stdin is _MISSING:
        stdin = None
    env = {**os.environ, **(spec.get("env") or {})}
    try:
        proc = subprocess.run(argv, input=(stdin if isinstance(stdin, str) else json.dumps(stdin)).encode()
                              if stdin is not None else b"", capture_output=True, cwd=scratch, env=env,
                              timeout=float(payload.get("timeout", 60)))
    except FileNotFoundError:
        return {"ok": False, "error_type": "operation_failed", "code": "test_failed",
                "error": f"executable not found: {argv[0]}", "reason": f"executable not found: {argv[0]}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error_type": "timeout", "code": "timeout", "error": "the command timed out",
                "reason": "the command timed out"}
    out, err = proc.stdout[:8_000_000], proc.stderr[-4000:].decode("utf-8", "replace")
    if proc.returncode != 0:
        msg = f"the command exited with code {proc.returncode}: {err.strip()[-600:]}"
        return {"ok": False, "error_type": "operation_failed", "code": "test_failed", "error": msg[:900],
                "reason": msg[:900], "exit_code": proc.returncode, "stderr": err[-1500:]}
    mode = spec.get("output", "stdout")
    try:
        if mode == "stdout":
            result: Any = {"stdout": out.decode("utf-8", "replace"), "exit_code": proc.returncode}
        elif mode == "stdout_auto":  # text when it is text, base64 when the program writes binary
            try:
                result = {"stdout": out.decode("utf-8"), "exit_code": proc.returncode}
            except UnicodeDecodeError:
                import base64
                result = {"stdout_base64": base64.b64encode(out).decode(), "bytes": len(out),
                          "exit_code": proc.returncode}
        elif mode == "stdout_json":
            result = {"result": json.loads(out.decode("utf-8"))}
        else:
            kind, _, rel = mode.partition(":")
            path = os.path.join(scratch, "out", rel)
            data = open(path, "rb").read(8_000_000)
            if kind == "file_base64":
                import base64
                result = {"file": rel, "base64": base64.b64encode(data).decode(), "bytes": len(data)}
            else:
                result = {"file": rel, "content": data.decode("utf-8", "replace"), "bytes": len(data)}
    except (OSError, ValueError) as exc:
        return {"ok": False, "error_type": "operation_failed", "code": "unsupported_output",
                "error": f"the output couldn't be read: {exc}"[:500], "reason": f"the output couldn't be read: {exc}"[:500]}
    return {"ok": True, "output": result}


def _shell_blocks(readme: str) -> List[str]:
    blocks = re.findall(r"```(?:sh|bash|shell|console|shell-session|zsh|text|terminal)?[^\n]*\n(.*?)```", readme, re.S)
    blocks += re.findall(r"::\n\n((?:[ \t]+.*\n|\n)+)", readme)
    blocks += re.findall(r"(?m)^\.\. (?:code|code-block|sourcecode)::[^\n]*\n(?:[ \t]+:\w[\w-]*:.*\n)*\n"
                         r"((?:[ \t]+.*\n|[ \t]*\n)+)", readme)
    return blocks[:60]


def _cli_examples(readme: str, bins: List[str]) -> List[dict]:
    """Commands from README shell blocks that call one of the tools, with stdin piped from echo/printf
    and the documented output (lines after a prompted command)."""
    import shlex
    out = []
    for block in _shell_blocks(readme):
        lines = [l.rstrip() for l in block.splitlines()]
        prompted = any(re.match(r"^\s*[$%>]\s", l) for l in lines)
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            i += 1
            m = re.match(r"^[$%>]\s+(.*)$", line)
            if prompted and not m:
                continue
            cmd = (m.group(1) if m else line).strip()
            stdin = None
            pipe = re.match(r"^(echo(?:\s+-n)?|printf)\s+(.+?)\s*\|\s*(.+)$", cmd)
            if pipe:
                try:
                    words = shlex.split(pipe.group(2))
                except ValueError:
                    continue
                text = " ".join(words)
                stdin = text if pipe.group(1) in ("printf", "echo -n") else text + "\n"
                if pipe.group(1) == "printf":
                    stdin = stdin.encode().decode("unicode_escape")
                cmd = pipe.group(3)
            if re.search(r"[|&;<>`]|\$\(", cmd):
                continue
            try:
                words = shlex.split(cmd)
            except ValueError:
                continue
            if not words or words[0] not in bins:
                continue
            expected = []
            if prompted:
                while i < len(lines) and not re.match(r"^\s*[$%>]\s", lines[i]):
                    expected.append(lines[i])
                    i += 1
            exp = "\n".join(expected).strip() or None
            out.append({"bin": words[0], "args": words[1:], "stdin": stdin, "expected": exp})
    return out[:20]


FILTER_SAMPLE = "Hello from Cognex\n"


def _probe_filters(bins: dict, payload: dict, tried: Optional[list] = None) -> Optional[dict]:
    """No documented example: many command-line programs are filters (standard input in, standard
    output out). Run each with sample text and no arguments; the first that answers becomes the
    operation, with binary output (e.g. a compressor) returned as base64."""
    repo = re.sub(r"[^a-z0-9]", "", (payload.get("repo_name") or "").lower())
    order = sorted(bins, key=lambda b: (re.sub(r"[^a-z0-9]", "", b.lower()) != repo, len(b)))
    for name in order[:4]:
        spec = {"argv": [f"{{bin:{name}}}", "{args...}"], "stdin": "{stdin}", "bins": bins, "output": "stdout_auto",
                "env": payload.get("env") or {}}
        inputs = {"args": [], "stdin": FILTER_SAMPLE}
        res = command({"spec": spec, "inputs": inputs, "scratch": payload["scratch"], "timeout": 20})
        out = res.get("output") or {}
        if res.get("ok") and (out.get("stdout") or "").strip() or out.get("stdout_base64"):
            return {"ok": True, "bin": name, "sample_inputs": inputs, "sample_output": out, "expected_checked": False,
                    "filter": True, "output": "stdout_auto"}
        if tried is not None:  # exactly what the program said, so the blocker is visible
            tried.append({"command": f"{name} < {FILTER_SAMPLE.strip()!r}", "exit_code": res.get("exit_code", 0),
                          "stderr": (res.get("stderr") or "")[-600:],
                          "result": res.get("reason") or "exit code 0 but no output"})
    return None


def cli_discover(payload: dict) -> dict:
    """Find a documented command-line example for one of the tools and run it as the sample."""
    bins: dict = payload["bins"]
    examples = _cli_examples(payload.get("readme") or "", list(bins))
    if payload.get("entry_point"):
        examples = [e for e in examples if e["bin"] == payload["entry_point"]]
    examples.sort(key=lambda e: (e["expected"] is None, e["stdin"] is None, len(e["args"])))
    tried = []
    for ex in examples[:6]:
        spec = {"argv": [f"{{bin:{ex['bin']}}}", "{args...}"], "stdin": "{stdin}", "bins": bins,
                "output": "stdout", "env": payload.get("env") or {}}
        inputs = {"args": ex["args"], **({"stdin": ex["stdin"]} if ex["stdin"] is not None else {})}
        res = command({"spec": spec, "inputs": inputs, "scratch": payload["scratch"], "timeout": 60})
        if not res.get("ok"):
            tried.append(f"{ex['bin']} {' '.join(ex['args'])[:80]}: {res.get('reason', '')[:120]}")
            continue
        stdout = res["output"]["stdout"]
        if ex["expected"] is not None and " ".join(stdout.split()) != " ".join(ex["expected"].split()):
            tried.append(f"{ex['bin']} {' '.join(ex['args'])[:80]}: printed {stdout.strip()[:80]!r}, the README "
                         f"shows {ex['expected'][:80]!r}")
            continue
        if not stdout.strip():
            tried.append(f"{ex['bin']} {' '.join(ex['args'])[:80]}: printed nothing")
            continue
        return {"ok": True, "bin": ex["bin"], "sample_inputs": inputs, "sample_output": res["output"],
                "expected_checked": ex["expected"] is not None, "examples_found": len(examples)}
    filt = _probe_filters(bins, payload, tried)
    if filt is not None:
        return filt
    probes = [t for t in tried if isinstance(t, dict)]
    return {"ok": False, "code": "needs_configuration", "tried": tried[:10],
            "reason": (("No runnable example of " + ", ".join(sorted(bins)) + " was found in the README"
                        if not examples else "None of the README examples of " + ", ".join(sorted(bins))
                        + " ran as documented: " + "; ".join(str(t) for t in tried[:3] if isinstance(t, str)))
                       + ("; run as a standard-input filter: " + "; ".join(
                           f"{p['command']} exited {p['exit_code']}: {(p['stderr'] or p['result']).strip()[-200:]}"
                           for p in probes[:3]) if probes else ""))[:1200]}


# -- Node.js (JavaScript / TypeScript) ----------------------------------------------------------------
def node(payload: dict) -> dict:
    """Run app/imports/node_runner.mjs (discover / call) under the same lockdown."""
    import subprocess
    runner_js = os.path.join(payload["app_dir"], "app", "imports", "node_runner.mjs")
    flags = ["--max-old-space-size=" + str(payload.get("heap_mb", 1536)), "--disable-warning=ExperimentalWarning"]
    if payload.get("typescript"):
        flags.append("--experimental-strip-types")
    try:
        proc = subprocess.run([payload["node"], *flags, runner_js], input=json.dumps(payload).encode(),
                              capture_output=True, cwd=payload["scratch"], timeout=float(payload.get("timeout", 120)))
    except subprocess.TimeoutExpired:
        return {"ok": False, "error_type": "timeout", "code": "timeout", "error": "timed out", "reason": "timed out"}
    for line in reversed(proc.stdout.decode("utf-8", "replace").splitlines()):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    tail = proc.stderr.decode("utf-8", "replace")[-800:]
    return {"ok": False, "error_type": "worker_crashed", "code": "sandbox_crashed",
            "error": f"node exited with code {proc.returncode}: {tail}"[:900],
            "reason": f"node exited with code {proc.returncode}: {tail}"[:900]}


def serve(payload: dict) -> dict:
    """Run an imported HTTP service until the parent stops it (seccomp "serve": listen, never connect)."""
    import subprocess
    env = {**os.environ, **{k: str(v) for k, v in (payload.get("env") or {}).items()}}
    env["HOME"] = env["TMPDIR"] = payload["scratch"]
    proc = subprocess.run([str(a) for a in payload["argv"]], cwd=payload.get("cwd") or payload["scratch"], env=env,
                          stdin=subprocess.DEVNULL)
    return {"ok": False, "code": "service_failed", "reason": f"the service exited with code {proc.returncode}"}


MODES = {"discover": discover, "call": call, "selftest": selftest, "chain": chain, "exec": exec_steps, "serve": serve,
         "command": command, "cli_discover": cli_discover, "node": node}

