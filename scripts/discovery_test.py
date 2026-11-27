"""Run every example GET /v1/operations publishes, exactly as an app or AI builder would copy it.

Usage:  COGNEX_API_KEY=... python scripts/discovery_test.py [BASE_URL]   (default: production)

For each operation: the request_example must succeed with the same operation and an output
matching response_example (numbers within 1%), and the plain_english_example must route to it.
Reads the key from the environment or .env, and never prints it.
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = (sys.argv[1] if len(sys.argv) > 1 else "https://vendor-production-41cd558f.up.railway.app").rstrip("/")


def key() -> str:
    for name in ("COGNEX_API_KEY", "API_KEY"):
        if os.environ.get(name):
            return os.environ[name]
    env = ROOT / ".env"
    if env.is_file():
        for line in env.read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() in ("COGNEX_API_KEY", "API_KEY") and v.strip():
                return v.strip().strip('"').strip("'")
    sys.exit("set COGNEX_API_KEY (a customer key) or API_KEY")


def http(method: str, path: str, body=None, headers=None, timeout=300):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as exc:
        return exc.code, {"error": exc.read().decode()[:300]}


def same_png(a: str, b: str) -> bool:
    """Images compare by their pixels: the same image can compress to different PNG bytes per platform."""
    try:
        from PIL import Image
        img = lambda s: Image.open(io.BytesIO(base64.b64decode(s)))  # noqa: E731
        x, y = img(a), img(b)
        return x.size == y.size and x.convert("RGBA").tobytes() == y.convert("RGBA").tobytes()
    except Exception:  # noqa: BLE001 - not decodable: fall back to exact text
        return a == b


def close(a, b, key: str = "") -> bool:
    if key.endswith("_png_base64") and isinstance(a, str) and isinstance(b, str):
        return same_png(a, b)
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(a, b, rel_tol=1e-2, abs_tol=1e-6)
    if isinstance(a, dict) and isinstance(b, dict):  # elapsed-time fields (e.g. solve_time_ms) vary per run
        return a.keys() == b.keys() and all(close(a[k], b[k], k) for k in a if not k.endswith(("_ms", "_seconds")))
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(close(x, y) for x, y in zip(a, b))
    return a == b


def main() -> None:
    k = key()
    st, cat = http("GET", "/v1/operations")
    assert st == 200, (st, cat)
    print(f"{BASE}: {cat['count']} operations, catalog_version {cat['catalog_version']}")
    failed = 0
    for op in cat["operations"]:
        name, problems = op["operation"], []
        st, res = http("POST", "/v1/execute", op["request_example"]["body"], {"X-API-Key": k})
        r = (res.get("results") or [{}])[0]
        want = op["response_example"]["results"][0]["output"]
        if st != 200 or r.get("status") != "ok" or r.get("operation") != name:
            problems.append(f"request_example: HTTP {st} {r.get('status')} {r.get('error_type', '')} {r.get('error', '')}")
        elif not close(r["output"], want):
            problems.append(f"request_example output differs: {json.dumps(r['output'])[:200]}")
        st, res = http("POST", "/v1/execute", op["plain_english_example"], {"X-API-Key": k})
        r = (res.get("results") or [{}])[0]
        if r.get("status") != "ok" or r.get("operation") != name:
            problems.append(f"plain_english_example: {r.get('status')} -> {r.get('operation')} "
                            f"{r.get('error') or r.get('question') or ''}")
        failed += bool(problems)
        print(f"[{'FAIL' if problems else 'PASS'}] {name}" + "".join(f"\n       {p}" for p in problems))
    print(f"\n{len(cat['operations']) - failed}/{len(cat['operations'])} operations: examples work as published")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
