"""RapidFuzz: string similarity and ranked list matching. Runs inline in the API process (C++)."""
from __future__ import annotations

from typing import Any, Dict

from rapidfuzz import fuzz, process, utils

from app.errors import OperationError
from app.ops._validate import choice, integer, number, require, string

SCORERS = {"WRatio": fuzz.WRatio, "ratio": fuzz.ratio, "partial_ratio": fuzz.partial_ratio,
           "token_sort_ratio": fuzz.token_sort_ratio, "token_set_ratio": fuzz.token_set_ratio}
MAX_CHOICES = 100_000


def compare(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Similarity of two strings, 0-100, via rapidfuzz.fuzz.ratio (normalized Indel similarity)."""
    return {"score": fuzz.ratio(string(inputs, "a"), string(inputs, "b")), "scorer": "ratio"}


def extract(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Rank `choices` by similarity to `query` via rapidfuzz.process.extract (best first)."""
    query = string(inputs, "query")
    choices = require(inputs, "choices")
    if not isinstance(choices, list) or not choices or not all(isinstance(c, str) for c in choices):
        raise OperationError("input 'choices' must be a non-empty list of strings")
    if len(choices) > MAX_CHOICES:
        raise OperationError(f"input 'choices' has more than {MAX_CHOICES} items")
    limit = integer(inputs, "limit", 5, 1, 1000)
    scorer = choice(inputs, "scorer", "WRatio", list(SCORERS))
    cutoff = number(inputs, "score_cutoff", 0.0, 0.0, 100.0)
    # default_process: lowercase, strip non-alphanumerics, so "JOHN SMITH." matches "john smith"
    found = process.extract(query, choices, scorer=SCORERS[scorer], processor=utils.default_process,
                            limit=limit, score_cutoff=cutoff)
    return {"query": query, "scorer": scorer,
            "matches": [{"choice": c, "score": round(float(s), 4), "index": int(i)} for c, s, i in found]}


OPERATIONS = {"rapidfuzz.compare": compare, "rapidfuzz.extract": extract}
