"""Discovery for apps and AI app builders: what each operation is for and exactly how to call it.

GET /v1/operations describes every operation the caller can run (built-in, published from GitHub
imports, and the caller's own private imports) in one shape, generated from the manifests, so a
newly published algorithm appears as soon as it is live (within the published-imports cache TTL).
Everything runs through the existing POST /v1/execute.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Optional

from app import catalog
from app.manifests import Capability

KEY_PLACEHOLDER = "<your Cognex API key>"
_SCHEMA_DETAIL = ("enum", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "minLength", "maxLength",
                  "minItems", "maxItems", "items", "pattern", "format")


def base_url(request) -> str:
    explicit = os.environ.get("COGNEX_PUBLIC_URL")
    if explicit:
        return explicit.rstrip("/")
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "localhost"
    proto = request.headers.get("x-forwarded-proto") or ("http" if host.startswith(("127.", "localhost")) else "https")
    return f"{proto}://{host}"


def _type(prop: Dict[str, Any]) -> str:
    t = prop.get("type")
    if isinstance(t, list):
        return " or ".join(t)
    if t:
        return t
    if "enum" in prop:
        return "string" if all(isinstance(v, str) for v in prop["enum"]) else "enum"
    for key in ("oneOf", "anyOf"):
        if key in prop:
            return " or ".join(dict.fromkeys(_type(p) for p in prop[key]))
    return "any"


def _field(name: str, prop: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"name": name, "type": _type(prop)}
    if prop.get("description"):
        out["description"] = prop["description"]
    if "default" in prop:
        out["default"] = prop["default"]
    out.update({k: prop[k] for k in _SCHEMA_DETAIL if k in prop})
    if "enum" in out:
        out["allowed"] = out.pop("enum")
    return out


def when_to_use(cap: Capability) -> str:
    if cap.when_to_use:
        return cap.when_to_use
    summary = cap.summary.rstrip(".")
    return f"Use when you need this: {summary[0].lower() + summary[1:]}."


def describe(cap: Capability, base: str, private: bool = False) -> Dict[str, Any]:
    """One operation, as an app or AI builder needs it."""
    schema = cap.input_schema
    props: Dict[str, Any] = schema.get("properties", {})
    required = list(schema.get("required", []))
    output = {k: v for k, v in (cap.example.get("output") or {}).items() if k not in cap.engine_fields}
    body = {"tasks": [{"id": "1", "operation": cap.name, "inputs": cap.example["inputs"]}]}
    item: Dict[str, Any] = {
        "operation": cap.name,
        "purpose": cap.summary,
        "when_to_use": when_to_use(cap),
        "credits": catalog.CREDITS_PER_OPERATION,
        "required_inputs": [_field(n, props.get(n, {})) for n in required],
        "optional_settings": [_field(n, p) for n, p in props.items() if n not in required] + [{
            "name": "timeout_s", "type": "number", "where": "task (next to operation and inputs)",
            "default": cap.default_timeout, "maximum": cap.max_timeout,
            "description": "Seconds before the run is stopped; capped at the maximum."}],
        "input_schema": schema,
        **({"output_schema": cap.output_schema} if cap.output_schema else {}),
        "request_example": {
            "method": "POST", "url": f"{base}/v1/execute",
            "headers": {"Content-Type": "application/json", "X-API-Key": KEY_PLACEHOLDER},
            "body": body},
        "response_example": {"results": [{"id": "1", "operation": cap.name, "status": "ok", "output": output}]},
        "plain_english_example": {"tasks": [{"id": "1", "request": cap.example["request"],
                                             "data": cap.example["data"]}]},
        # kept for existing integrations
        "summary": cap.summary,
        "default_timeout_s": cap.default_timeout, "max_timeout_s": cap.max_timeout,
        "example": {"request": cap.example["request"], "data": cap.example["data"]},
    }
    if private:
        item["private"] = True
    return item


def how_to_call(base: str, max_tasks: int) -> Dict[str, Any]:
    return {
        "endpoint": f"{base}/v1/execute", "method": "POST",
        "headers": {"Content-Type": "application/json", "X-API-Key": KEY_PLACEHOLDER},
        "body": {"tasks": [{"id": "your id for this task", "operation": "<operation>", "inputs": {"...": "..."},
                            "timeout_s": "optional"}]},
        "plain_english": "Instead of operation + inputs, a task may send request (what you want, in plain "
                         "English) + data; Cognex picks the operation, or answers status needs_clarification "
                         "with one question (free).",
        "batch": f"Up to {max_tasks} tasks per request run concurrently; results come back in the same order.",
        "credits": f"{catalog.CREDITS_PER_OPERATION} credit per task that runs. Unknown operations, invalid "
                   f"inputs and clarifications are free. 402 insufficient_credits means nothing ran.",
        "result": "Each result has id, operation, status (ok | error | needs_clarification) and output, or "
                  "error_type (invalid_input, unknown_operation, timeout, operation_failed, ...) and error.",
        "keys": "Each customer uses their own API key; never put it in browser code.",
    }


def version(operations: List[Dict[str, Any]]) -> str:
    """Changes whenever an operation is added, removed or changed (also used as the ETag)."""
    return hashlib.sha256(json.dumps(operations, sort_keys=True, default=str).encode()).hexdigest()[:16]


def find(operations: List[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    public = catalog.canonical(name)
    return next((o for o in operations if o["operation"] in (name, public)), None)
