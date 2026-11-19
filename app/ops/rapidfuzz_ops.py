"""RapidFuzz: string similarity. Runs inline in the API process (fast, pure C++)."""
from __future__ import annotations

from typing import Any, Dict

from rapidfuzz import fuzz

from app.ops._validate import string


def compare(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Similarity of two strings, 0-100, via rapidfuzz.fuzz.ratio (normalized Indel similarity)."""
    return {"score": fuzz.ratio(string(inputs, "a"), string(inputs, "b")), "scorer": "ratio"}


OPERATIONS = {"rapidfuzz.compare": compare}
