"""Plain-English routing: pick a registered operation for a request like "Compare these two names".

Deterministic and local (no model calls, customer data never leaves the server). Everything it
knows comes from the capability manifests, so a newly registered capability is routable as
soon as it is registered; this file contains no operation names.

1. Score the request against each capability's intents and summary (idf-weighted overlap of
   stemmed words; words shared by many capabilities count less, unknown words count a little).
2. Bind the customer's `data` to each plausible capability's input schema:
   - data that already has the input names (or a declared `x-aliases` synonym), or
   - a manifest `data_bindings` shape, e.g. a list of two strings -> {"a": ..., "b": ...}.
   The bound inputs must validate against the input schema.
3. Run the best match whose data binds. Otherwise ask exactly one short question: which
   operation was meant (unclear goal, or two equally good matches), or what data is missing.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

import jsonschema
from jsonschema.exceptions import best_match

from app.manifests import Capability

MIN_SCORE = 0.22      # below this, the request doesn't clearly ask for any capability
CANDIDATE_RATIO = 0.6  # capabilities scoring within this fraction of the best are considered
TIE_RATIO = 0.85      # two data-compatible matches this close: ask which one was meant
UNKNOWN_WEIGHT = 0.6  # weight of request words no capability mentions
MAX_REQUEST_CHARS = 2000

STOPWORDS = set("""a an the this these that those of in on for to and or with my me i we us our you your
please can could would will should it its is are be been was were from by as at which what how do does did
some any all each give get show tell want need like using use here there so just now then also about into
data set ok hi hello thanks thank let lets let's""".split())


def _stem(word: str) -> str:
    for suffix, keep in (("ities", 4), ("ity", 4), ("ies", 3), ("ing", 4), ("est", 4), ("ers", 4), ("er", 4),
                         ("ed", 4), ("es", 4), ("ly", 4), ("s", 3)):
        if word.endswith(suffix) and len(word) - len(suffix) >= keep:
            word = word[: -len(suffix)] + ("y" if suffix == "ies" else "")
            break
    return word[:-1] if len(word) > 4 and word.endswith("e") else word


def tokens(text: str) -> List[str]:
    words = re.findall(r"[a-z0-9]+", text.lower().replace("-", " ").replace("_", " "))
    return [_stem(w) for w in words if w not in STOPWORDS]


# -- binding data to inputs ------------------------------------------------------------------------
_validators: Dict[int, Tuple[Capability, jsonschema.Draft202012Validator]] = {}


def _validator(cap: Capability) -> jsonschema.Draft202012Validator:
    hit = _validators.get(id(cap))
    if hit is None or hit[0] is not cap:
        hit = (cap, jsonschema.Draft202012Validator(cap.input_schema))
        _validators[id(cap)] = hit
    return hit[1]


SAMPLE_EDGE = 4  # long lists are checked on their first and last 4 items


def sample(value: Any) -> Any:
    """A cheap stand-in for schema checks: long lists keep only their first and last items.

    Keeps validation O(size of the sample), so a 10,000 x 768 matrix is checked in microseconds.
    Full sizes and every element are still checked by the adapter when the operation runs.
    """
    if isinstance(value, list):
        if len(value) > 2 * SAMPLE_EDGE:
            value = value[:SAMPLE_EDGE] + value[-SAMPLE_EDGE:]
        return [sample(v) for v in value]
    if isinstance(value, dict):
        return {k: sample(v) for k, v in value.items()}
    return value


def validation_error(cap: Capability, inputs: Any) -> Optional[str]:
    """None if `inputs` satisfies the capability's input schema, else a short message."""
    err = best_match(_validator(cap).iter_errors(sample(inputs)))
    if err is None:
        return None
    where = "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in err.absolute_path).lstrip(".")
    return f"input '{where}': {err.message}" if where else err.message


def _norm(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def _pointer(data: Any, ptr: str) -> Any:
    """Resolve a JSON Pointer ("" = the whole value). Raises LookupError if absent."""
    if ptr == "":
        return data
    cur = data
    for part in ptr.lstrip("/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(cur, list):
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            cur = cur[part]
        else:
            raise LookupError(ptr)
    return cur


def _rename(cap: Capability, data: Dict[str, Any]) -> Dict[str, Any]:
    """Map customer keys onto input names using the names themselves and their x-aliases."""
    names: Dict[str, str] = {}
    for prop, spec in cap.input_schema.get("properties", {}).items():
        names[_norm(prop)] = prop
        for alias in spec.get("x-aliases", []) if isinstance(spec, dict) else []:
            names.setdefault(_norm(alias), prop)
    out: Dict[str, Any] = {}
    for key, value in data.items():
        prop = key if key in cap.input_schema.get("properties", {}) else names.get(_norm(key))
        if prop is not None and prop not in out:
            out[prop] = value
    return out


@dataclass
class Binding:
    inputs: Optional[Dict[str, Any]]  # None when the data doesn't fit
    problem: Optional[str] = None
    missing: List[str] = field(default_factory=list)


def bind(cap: Capability, data: Any) -> Binding:
    candidates: List[Dict[str, Any]] = []
    sampled = sample(data)
    renamed = _rename(cap, data) if isinstance(data, dict) else {}
    if isinstance(data, dict):
        candidates.append(renamed)
    for b in cap.data_bindings:
        if jsonschema.Draft202012Validator(b["when"]).is_valid(sampled):
            try:
                mapped = {k: _pointer(data, p) for k, p in b["inputs"].items()}
            except (LookupError, ValueError, IndexError):
                continue
            candidates.append({**renamed, **mapped})
    required = cap.input_schema.get("required", [])
    best: Optional[Binding] = None
    for inputs in candidates or [{}]:
        problem = validation_error(cap, inputs)
        if problem is None:
            return Binding(inputs)
        missing = [r for r in required if r not in inputs]
        if best is None or len(missing) < len(best.missing):
            best = Binding(None, problem, missing)
    return best


# -- scoring ---------------------------------------------------------------------------------------
def _idf(caps: List[Capability]) -> Dict[str, float]:
    df: Dict[str, int] = {}
    for cap in caps:
        for t in set(tokens(" ".join((*cap.intents, cap.summary, cap.name)))):
            df[t] = df.get(t, 0) + 1
    n = len(caps)
    return {t: math.log(1 + n / d) for t, d in df.items()}


def _sim(q: List[str], phrase: List[str], idf: Dict[str, float], unknown: float) -> float:
    if not q or not phrase:
        return 0.0
    w = lambda t: idf.get(t, unknown)  # noqa: E731
    qs, ps = set(q), set(phrase)
    shared = sum(w(t) ** 2 for t in qs & ps)
    norm = math.sqrt(sum(w(t) ** 2 for t in qs)) * math.sqrt(sum(w(t) ** 2 for t in ps))
    return shared / norm if norm else 0.0


def score(request: str, caps: List[Capability]) -> List[Tuple[float, Capability, str]]:
    """(score 0-1, capability, best-matching intent) for every capability, best first."""
    idf = _idf(caps)
    unknown = UNKNOWN_WEIGHT * (max(idf.values()) if idf else 1.0)
    q = tokens(request)
    out = []
    for cap in caps:
        phrases = [(i, tokens(i)) for i in cap.intents]
        best_intent, best = max(((i, _sim(q, t, idf, unknown)) for i, t in phrases), key=lambda x: x[1])
        summary = _sim(q, tokens(cap.summary + " " + cap.name.replace(".", " ")), idf, unknown)
        out.append((round(max(best, 0.8 * summary), 4), cap, best_intent))
    return sorted(out, key=lambda x: (-x[0], x[1].name))


# -- decision --------------------------------------------------------------------------------------
@dataclass
class Decision:
    capability: Optional[Capability] = None
    inputs: Optional[Dict[str, Any]] = None
    question: Optional[str] = None
    options: List[str] = field(default_factory=list)
    matched: Optional[str] = None
    confidence: float = 0.0

    @property
    def runnable(self) -> bool:
        return self.capability is not None and self.inputs is not None


def _short(cap: Capability) -> str:
    s = re.split(r" \(|: |; |, ", cap.summary, maxsplit=1)[0].rstrip(". ")
    return s[0].lower() + s[1:]


def _either(caps: List[Capability]) -> str:
    parts = [_short(c) for c in caps]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " or " + parts[-1]


def route(request: str, data: Any, caps: Iterable[Capability]) -> Decision:
    caps = list(caps)
    if not caps:
        return Decision(question="No operations are available right now.")
    ranked = score(request[:MAX_REQUEST_CHARS], caps)
    best_score = ranked[0][0]
    if best_score < MIN_SCORE:
        # Unclear goal: suggest the operations the data could feed, else the closest by wording.
        fits = [c for _, c, _ in ranked if data is not None and bind(c, data).inputs is not None]
        suggest = (fits or [c for _, c, _ in ranked])[:3]
        question = (f"Do you want to {_short(suggest[0])}?" if len(suggest) == 1 else
                    f"What would you like to do? For example: {_either(suggest)}.")
        return Decision(question=question, options=[c.name for c in suggest], confidence=best_score)

    candidates = [(s, c, i) for s, c, i in ranked if s >= max(MIN_SCORE, best_score * CANDIDATE_RATIO)]
    bound = [(s, c, i, b) for s, c, i in candidates if (b := bind(c, data)).inputs is not None]
    if bound:
        s, cap, intent, b = bound[0]
        if len(bound) > 1 and bound[1][0] >= s * TIE_RATIO:
            pair = [cap, bound[1][1]]
            return Decision(question=f"Do you want to {_either(pair)}?", options=[c.name for c in pair],
                            confidence=s)
        return Decision(capability=cap, inputs=b.inputs, matched=intent, confidence=s)

    s, cap, intent = candidates[0]  # the goal is clear but the data doesn't fit it
    b = bind(cap, data)
    if cap.clarify:
        question = cap.clarify
    elif b.missing:
        question = f"To {_short(cap)}, what should I use for {', '.join(b.missing)}? Send it in data."
    else:
        question = f"To {_short(cap)}, the data doesn't fit: {b.problem}. Can you correct it?"
    return Decision(capability=cap, question=question, options=[cap.name], matched=intent, confidence=s)
