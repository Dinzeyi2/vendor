"""Capability manifests: one JSON file per operation in app/capabilities/.

A manifest is everything Cognex needs to know about an operation: its public name, what it
does (summary + intents, used by the plain-English router), its input JSON Schema, how loose
customer `data` maps onto those inputs, an example, how to run it, and the open-source project
and pinned dependency behind it. The registry, catalog, router and open-source page are all
built from manifests, so adding a capability never requires editing them.

Loading manifests is cheap (JSON only): the API process never imports heavy libraries for it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import jsonschema

CAPABILITY_DIR = Path(__file__).resolve().parent / "capabilities"
NAME_RE = r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$"

# The format every manifest must follow (validated at load; see .claude/skills/add-github-repo).
MANIFEST_SCHEMA: Dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "summary", "intents", "runtime", "timeouts", "input_schema", "example", "built_on"],
    "properties": {
        "name": {"type": "string", "pattern": NAME_RE},
        "aliases": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
        "summary": {"type": "string", "minLength": 10, "maxLength": 300},
        "intents": {"type": "array", "minItems": 3, "items": {"type": "string", "minLength": 3}},
        "clarify": {"type": "string", "minLength": 5, "maxLength": 300},
        "credits": {"const": 1},  # billing charges 1 credit per started operation
        "runtime": {
            "type": "object", "additionalProperties": False, "required": ["kind", "internal"],
            "properties": {
                "kind": {"enum": ["inline", "python", "node"]},
                "internal": {"type": "string"},          # the name inside the adapter's OPERATIONS
                "module": {"type": "string"},            # inline / python: adapter module
                "worker": {"type": "string"},            # python / node: worker process name (shared ok)
                "script": {"type": "string"},            # node: worker script, relative to the repo root
                "env": {"type": "object", "additionalProperties": {"type": "string"}},
                "env_profile": {"enum": ["java"]},
            },
        },
        "timeouts": {
            "type": "object", "additionalProperties": False, "required": ["default", "max"],
            "properties": {"default": {"type": "number", "exclusiveMinimum": 0},
                           "max": {"type": "number", "exclusiveMinimum": 0}},
        },
        "input_schema": {"type": "object", "required": ["type", "properties"],
                         "properties": {"type": {"const": "object"}}},
        "data_bindings": {
            "type": "array",
            "items": {"type": "object", "additionalProperties": False, "required": ["when", "inputs"],
                      "properties": {"when": {"type": "object"},
                                     "inputs": {"type": "object", "additionalProperties": {"type": "string"}}}},
        },
        "example": {
            "type": "object", "additionalProperties": False, "required": ["request", "data", "inputs"],
            "properties": {"request": {"type": "string"}, "data": {}, "inputs": {"type": "object"},
                           "output": {"type": "object"}},
        },
        "engine_fields": {"type": "array", "items": {"type": "string"}},
        "built_on": {
            "type": "object", "additionalProperties": False,
            "required": ["project", "license", "copyright", "source", "license_file", "dependency"],
            "properties": {
                "project": {"type": "string"}, "license": {"type": "string"},
                "copyright": {"type": "string"}, "source": {"type": "string", "pattern": "^https://"},
                "license_file": {"type": "string", "pattern": "^licenses/"},
                "dependency": {
                    "type": "object", "additionalProperties": False, "required": ["manager", "name", "version"],
                    "properties": {"manager": {"enum": ["pip", "npm"]}, "name": {"type": "string"},
                                   "version": {"type": "string", "minLength": 1},
                                   "commit": {"type": "string", "pattern": "^[0-9a-f]{40}$"}},
                },
            },
        },
    },
}


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class BuiltOn:
    project: str
    license: str
    copyright: str
    source: str
    license_file: str
    dependency: Dict[str, str]


@dataclass(frozen=True)
class Capability:
    name: str
    summary: str
    intents: Tuple[str, ...]
    runtime: Dict[str, Any]
    default_timeout: float
    max_timeout: float
    input_schema: Dict[str, Any]
    example: Dict[str, Any]
    built_on: BuiltOn
    aliases: Tuple[str, ...] = ()
    clarify: Optional[str] = None
    credits: int = 1
    data_bindings: Tuple[Dict[str, Any], ...] = ()
    engine_fields: Tuple[str, ...] = ()
    path: Optional[str] = field(default=None, compare=False)

    @property
    def internal(self) -> str:
        return self.runtime["internal"]


def parse(raw: Dict[str, Any], path: Optional[str] = None) -> Capability:
    where = path or raw.get("name", "?")
    try:
        jsonschema.validate(raw, MANIFEST_SCHEMA)
        jsonschema.Draft202012Validator.check_schema(raw["input_schema"])
        for b in raw.get("data_bindings", []):
            jsonschema.Draft202012Validator.check_schema(b["when"])
    except jsonschema.ValidationError as exc:
        raise ManifestError(f"{where}: {exc.json_path}: {exc.message}") from None
    except jsonschema.SchemaError as exc:
        raise ManifestError(f"{where}: invalid schema: {exc.message}") from None
    rt = raw["runtime"]
    need = {"inline": ["module"], "python": ["module", "worker"], "node": ["script", "worker"]}[rt["kind"]]
    missing = [k for k in need if k not in rt]
    if missing:
        raise ManifestError(f"{where}: runtime kind '{rt['kind']}' needs {missing}")
    t = raw["timeouts"]
    if t["default"] > t["max"]:
        raise ManifestError(f"{where}: timeouts.default exceeds timeouts.max")
    b = raw["built_on"]
    return Capability(
        name=raw["name"], summary=raw["summary"], intents=tuple(raw["intents"]), runtime=rt,
        default_timeout=float(t["default"]), max_timeout=float(t["max"]), input_schema=raw["input_schema"],
        example=raw["example"], aliases=tuple(raw.get("aliases", [])), clarify=raw.get("clarify"),
        credits=raw.get("credits", 1), data_bindings=tuple(raw.get("data_bindings", [])),
        engine_fields=tuple(raw.get("engine_fields", [])),
        built_on=BuiltOn(b["project"], b["license"], b["copyright"], b["source"], b["license_file"],
                         dict(b["dependency"])),
        path=path)


def load_dir(directory: Path = CAPABILITY_DIR) -> List[Capability]:
    caps = []
    for p in sorted(Path(directory).glob("*.json")):
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ManifestError(f"{p.name}: not valid JSON: {exc}") from None
        caps.append(parse(raw, str(p)))
    return caps


def check_unique(caps: List[Capability]) -> None:
    seen: Dict[str, str] = {}
    for c in caps:
        for n in (c.name, *c.aliases):
            if n in seen:
                raise ManifestError(f"name '{n}' is used by both {seen[n]} and {c.name}")
            seen[n] = c.name
