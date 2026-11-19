"""Registry of operations exposed by the unified algorithm API."""
from __future__ import annotations

from typing import Any, Callable, Dict

from rapidfuzz import fuzz


class OperationError(ValueError):
    """Raised when an operation receives invalid inputs."""


def _require_str(inputs: Dict[str, Any], key: str) -> str:
    if key not in inputs:
        raise OperationError(f"missing required input '{key}'")
    value = inputs[key]
    if not isinstance(value, str):
        raise OperationError(f"input '{key}' must be a string")
    return value


def rapidfuzz_compare(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Similarity of two strings (0-100) using rapidfuzz.fuzz.ratio."""
    a = _require_str(inputs, "a")
    b = _require_str(inputs, "b")
    return {"score": fuzz.ratio(a, b), "scorer": "ratio"}


OPERATIONS: Dict[str, Callable[[Dict[str, Any]], Dict[str, Any]]] = {
    "rapidfuzz.compare": rapidfuzz_compare,
}
