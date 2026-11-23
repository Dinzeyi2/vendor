"""Static checks every capability manifest must pass (pytest and scripts/verify_capabilities.py).

No heavy imports: works on manifests, the router and files in the repo.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List

from app import router
from app.config import ROOT
from app.manifests import Capability


def _requirements() -> List[str]:
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return [l.split("#", 1)[0].strip() for l in lines if l.split("#", 1)[0].strip()]


def _pinned(cap: Capability) -> bool:
    dep = cap.built_on.dependency
    name, version = dep["name"].lower(), dep["version"]
    if dep["manager"] == "pip":
        for line in _requirements():
            req = line.split(";", 1)[0].strip().lower()
            if req.startswith(f"{name}=={version.lower()}"):
                return True
            if req.startswith(f"{name} @ git+") and dep.get("commit") and req.endswith("@" + dep["commit"]):
                return True
        return False
    for pkg in ROOT.glob("workers/*/package.json"):
        deps = json.loads(pkg.read_text(encoding="utf-8")).get("dependencies", {})
        if deps.get(dep["name"]) == version:
            return True
    return False


def library_words(caps: List[Capability]) -> List[str]:
    """Names customers must never see in public text: projects, packages and original names."""
    words = set()
    for c in caps:
        words.add(c.built_on.project.split()[0].lower())
        words.add(re.split(r"[/@]", c.built_on.dependency["name"].lower().lstrip("@"))[-1])
        words.update(a.split(".")[0].lower() for a in c.aliases)
        words.update(c.runtime["internal"].split(".")[:1])
    return sorted(w for w in words if w)


def public_text(cap: Capability) -> str:
    return " ".join([cap.name, cap.summary, cap.clarify or "", cap.example["request"], *cap.intents,
                     json.dumps(cap.input_schema)])


def check(cap: Capability, caps: List[Capability]) -> List[str]:
    """Problems with one capability, judged among all registered capabilities (empty = OK)."""
    problems = []
    bad = router.validation_error(cap, cap.example["inputs"])
    if bad:
        problems.append(f"example.inputs fail the input schema: {bad}")

    d = router.route(cap.example["request"], cap.example["data"], caps)
    if not d.runnable or d.capability.name != cap.name:
        got = d.capability.name if d.runnable else f"question: {d.question}"
        problems.append(f"example request {cap.example['request']!r} with example data routes to {got}")
    elif d.inputs != cap.example["inputs"]:
        problems.append(f"example data binds to {json.dumps(d.inputs)[:200]}, not to example.inputs")

    for intent in cap.intents:  # each phrase must lead here (with this capability's data)
        d = router.route(intent, cap.example["data"], caps)
        if not d.runnable or d.capability.name != cap.name:
            got = d.capability.name if d.runnable else f"a question ({d.question})"
            problems.append(f"intent {intent!r} routes to {got}")

    text = public_text(cap).lower()
    leaked = [w for w in library_words(caps) if re.search(rf"\b{re.escape(w)}\b", text)]
    if leaked:
        problems.append(f"public text mentions library names {leaked}")

    lic = ROOT / cap.built_on.license_file
    if not cap.built_on.license_file.startswith("licenses/") or not lic.is_file() or lic.stat().st_size < 200:
        problems.append(f"license file {cap.built_on.license_file} is missing or empty")
    if not _pinned(cap):
        dep = cap.built_on.dependency
        problems.append(f"dependency {dep['name']} {dep['version']} is not pinned exactly "
                        f"({'requirements.txt' if dep['manager'] == 'pip' else 'workers/*/package.json'})")
    rt = cap.runtime
    if rt["kind"] in ("inline", "python"):
        mod = ROOT / (rt["module"].replace(".", "/") + ".py")
        if not mod.is_file():
            problems.append(f"adapter module {rt['module']} not found")
        elif rt["internal"] not in mod.read_text(encoding="utf-8"):
            problems.append(f"adapter {rt['module']} does not define operation {rt['internal']!r}")
    elif not (ROOT / rt["script"]).is_file():
        problems.append(f"worker script {rt['script']} not found")
    return problems
