"""Run every request in docs/cognex-api-guide.md exactly as written and check the answers.

Usage:  COGNEX_API_KEY=... python scripts/guide_test.py [BASE_URL]
        (default: the Railway URL in the guide; pass http://127.0.0.1:8000 to test a local server)

Extracts each ```bash block that calls curl, plus the Python, JavaScript (Node 18+) and
PowerShell examples, swaps in BASE_URL if given, runs them, and verifies every result.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

GUIDE = Path(__file__).resolve().parent.parent / "docs" / "cognex-api-guide.md"
RAILWAY = "https://vendor-production-41cd558f.up.railway.app"


def ball_y(o):
    return next(b for b in o["bodies"] if b["id"] == "ball")["position"][1]


CHECKS = {
    "text.similarity": lambda o: abs(o["score"] - 96.296) < 0.01,
    "vectors.nearest_neighbors": lambda o: o["labels"] == [[1, 0]],
    "tabular.train_and_predict": lambda o: o["predictions"] == [0, 1],
    "optimization.solve_convex": lambda o: o["status"] == "solved" and abs(o["x"][0] - 1.6) < 1e-4
    and abs(o["x"][1] - 1.2) < 1e-4,
    "causal.estimate_effect": lambda o: abs(o["effect"] - 3.0) < 0.1,
    "image.detect_edges": lambda o: o["width"] == 48 and 60 <= o["edge_pixels"] <= 140 and o["edges_png_base64"],
    "image.extract_text": lambda o: o["text"].replace(" ", "").lower() == "hellocognex",
    "math.discover_equation": lambda o: o["best"]["loss"] < 1e-6,
    "scheduling.school_timetable": lambda o: o["feasible"] and all(a["timeslot_id"] and a["room_id"]
                                                                   for a in o["assignments"]),
    "physics.simulate_rigid_bodies": lambda o: abs(ball_y(o) - 0.6) < 0.05,
}


def blocks(text: str, lang: str):
    return re.findall(rf"```{lang}\n(.*?)\n```", text, flags=re.S)


def check_results(results) -> list[str]:
    problems = []
    for r in results:
        op = r["operation"]
        if r["status"] != "ok":
            problems.append(f"{r['id']} {op}: {r.get('error_type')}: {r.get('error')}")
        elif not CHECKS[op](r["output"]):
            problems.append(f"{r['id']} {op}: unexpected output {json.dumps(r['output'])[:200]}")
    return problems


def run(label: str, cmd: list[str], parse) -> bool:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1500, env=os.environ)
    try:
        problems = parse(proc.stdout) if proc.returncode == 0 else [f"exit {proc.returncode}: {proc.stderr[-500:]}"]
    except Exception as exc:
        problems = [f"{type(exc).__name__}: {exc}; stdout={proc.stdout[:300]!r} stderr={proc.stderr[-300:]!r}"]
    print(f"[{'PASS' if not problems else 'FAIL'}] {label}")
    for p in problems:
        print("       " + p)
    return not problems


def main() -> None:
    base = (sys.argv[1] if len(sys.argv) > 1 else RAILWAY).rstrip("/")
    if not os.environ.get("COGNEX_API_KEY"):
        sys.exit("set COGNEX_API_KEY")
    text = GUIDE.read_text(encoding="utf-8").replace(RAILWAY, base)
    named = re.findall(r"(?i)rapidfuzz|hnswlib|xgboost|dowhy|kornia|paddle|pysr|timefold|rapier|github|scs", text)
    assert not named, f"the guide must show only Cognex names; found {sorted(set(named))}"
    bash = shutil.which("bash") or sys.exit("bash is required to run the curl examples")
    results_ok = []

    curls = [b for b in blocks(text, "bash") if b.startswith("curl ")]
    assert len(curls) == 12, f"expected 12 curl examples (quickstart + 10 operations + combined), found {len(curls)}"
    seen = set()
    for block in curls:
        ops = re.findall(r'"operation": "([a-z_.]+)"', block)
        label = "curl " + (ops[0] if len(ops) == 1 else f"combined ({len(ops)} operations)")
        seen.update(ops)
        results_ok.append(run(label, [bash, "-c", block], lambda out: check_results(json.loads(out)["results"])))
    assert seen == set(CHECKS), f"operations without an example: {set(CHECKS) - seen}"

    def printed(out: str) -> list[str]:  # the code examples print "<id> <output>" for each result
        return [] if out.startswith("1 ") and "96.29" in out else [f"unexpected output {out[:200]!r}"]

    with tempfile.TemporaryDirectory() as tmp:
        (py,) = blocks(text, "python")
        Path(tmp, "example.py").write_text(py)
        results_ok.append(run("python example", [sys.executable, str(Path(tmp, "example.py"))], printed))
        (js,) = blocks(text, "javascript")
        Path(tmp, "cognex.mjs").write_text(js)
        node = os.environ.get("NODE_BIN") or shutil.which("node")
        if not node:  # the project-local runtime used by the physics worker
            sys.path.insert(0, str(GUIDE.parent.parent))
            from app.config import node_bin
            node = node_bin() if Path(node_bin()).exists() else None
        if node:
            results_ok.append(run("javascript example", [node, str(Path(tmp, "cognex.mjs"))], printed))
        else:
            print("[SKIP] javascript example (node not found)")
        ps = [b for b in blocks(text, "powershell") if "Invoke-RestMethod" in b]
        shell = shutil.which("powershell") or shutil.which("pwsh")
        if ps and shell:
            Path(tmp, "example.ps1").write_text(ps[0], encoding="utf-8-sig")
            results_ok.append(run("powershell example", [shell, "-NoProfile", "-ExecutionPolicy", "Bypass",
                                                         "-File", str(Path(tmp, "example.ps1"))],
                                  lambda out: check_results(json.loads(out) if out.strip().startswith("[")
                                                            else [json.loads(out)])))
        else:
            print("[SKIP] powershell example (PowerShell not found)")

    failed = results_ok.count(False)
    print(f"\n{'ALL GUIDE EXAMPLES PASSED' if not failed else f'{failed} EXAMPLE(S) FAILED'} against {base}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
