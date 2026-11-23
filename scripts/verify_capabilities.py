"""Check capability manifests; optionally run their examples against a live server.

Usage:
  python scripts/verify_capabilities.py                       # static checks, all capabilities
  python scripts/verify_capabilities.py --only text.rank_matches
  COGNEX_API_KEY=... python scripts/verify_capabilities.py --only NAME --live http://127.0.0.1:8000

Static checks (also run by pytest): the manifest format; the example validates against the input
schema; the example request + data routes to this capability and binds to example.inputs; each
intent routes here (no capability steals another's requests); no library names in public text;
the license file exists; the dependency is pinned exactly; the adapter exists.

Live checks: the example runs by name (operation + inputs) and by plain English (request + data),
returns status ok, and its output has every key of example.output.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import capability_checks, manifests  # noqa: E402


def post(base: str, key: str, task: dict) -> dict:
    req = urllib.request.Request(f"{base.rstrip('/')}/v1/execute", data=json.dumps({"tasks": [task]}).encode(),
                                 headers={"Content-Type": "application/json", "X-API-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=1200) as r:
            return json.loads(r.read())["results"][0]
    except urllib.error.HTTPError as exc:
        return {"status": f"HTTP {exc.code}", "error": exc.read().decode()[:300]}


def live(cap, base: str, key: str) -> list:
    problems = []
    want = set((cap.example.get("output") or {}).keys())
    for label, task in (("by name", {"id": "named", "operation": cap.name, "inputs": cap.example["inputs"]}),
                        ("plain English", {"id": "routed", "request": cap.example["request"],
                                           "data": cap.example["data"]})):
        res = post(base, key, task)
        if res.get("status") != "ok":
            problems.append(f"{label}: {res.get('status')} {res.get('error_type', '')} "
                            f"{res.get('error') or res.get('question')}")
            continue
        if res.get("operation") != cap.name:
            problems.append(f"{label}: ran {res.get('operation')}")
        missing = want - set(res["output"])
        if missing:
            problems.append(f"{label}: output lacks {sorted(missing)}")
    return problems


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="capability names to check (default: all)")
    ap.add_argument("--live", metavar="BASE_URL", help="also run the examples against this server")
    args = ap.parse_args()
    caps = manifests.load_dir()
    manifests.check_unique(caps)
    selected = [c for c in caps if not args.only or c.name in args.only]
    if args.only and len(selected) != len(set(args.only)):
        sys.exit(f"unknown capabilities: {sorted(set(args.only) - {c.name for c in caps})}")
    key = os.environ.get("COGNEX_API_KEY") or os.environ.get("API_KEY", "")
    if args.live and not key:
        sys.exit("--live needs COGNEX_API_KEY (or API_KEY)")
    failed = 0
    for cap in selected:
        problems = capability_checks.check(cap, caps)
        if args.live and not problems:
            problems += live(cap, args.live, key)
        failed += bool(problems)
        print(f"[{'FAIL' if problems else 'PASS'}] {cap.name}")
        for p in problems:
            print("       " + p)
    print(f"\n{len(selected) - failed}/{len(selected)} capabilities passed"
          + (f" (static + live against {args.live})" if args.live else " (static)"))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
