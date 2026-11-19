"""Real HTTP test of customer keys and usage, with two customer accounts.

Usage:
  WEBSITE_BACKEND_SECRET=... [API_KEY=<operator key>] python scripts/account_smoke_test.py BASE_URL

Creates two throwaway customers, checks auth and isolation, runs all 10 operations with a
customer key, verifies usage, revokes a key and confirms it gets 401, then runs the full
smoke test with a customer key. Secrets are never printed (only key prefixes).
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smoke_test  # noqa: E402  (shares BASE and the 10 known-answer CASES)

BASE = smoke_test.BASE
SERVER = os.environ["WEBSITE_BACKEND_SECRET"]
OPERATOR = os.environ.get("API_KEY", "")
failures = 0


def call(method, path, *, headers=None, body=None):
    req = urllib.request.Request(BASE + path, method=method, headers={"Content-Type": "application/json", **(headers or {})},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def site(customer, secret=SERVER):
    return {"Authorization": f"Bearer {secret}", "X-Customer-Id": customer}


def check(label, ok, detail=""):
    global failures
    failures += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))


def execute(key, tasks):
    return call("POST", "/v1/execute", headers={"X-API-Key": key}, body={"tasks": tasks})


FUZZ = {"id": "fuzz", "operation": "rapidfuzz.compare", "inputs": {"a": "Drift Protein", "b": "Drift Proteins"}}
ALL10 = [{"id": op.split(".")[0], "operation": op, "inputs": inp} for op, (inp, _, _) in smoke_test.CASES.items()]

run = secrets.token_hex(4)
A, B = f"smoketest-a-{run}", f"smoketest-b-{run}"
print(f"== {BASE}  customers: {A}, {B}")
print("health:", call("GET", "/health")[1])

print("\n== website-backend authentication")
check("no server credential -> 401", call("GET", "/v1/account/keys", headers={"X-Customer-Id": A})[0] == 401)
check("wrong server credential -> 401", call("GET", "/v1/account/keys", headers=site(A, "wrong" + SERVER))[0] == 401)
if OPERATOR:
    check("operator API_KEY as server credential -> 401", call("GET", "/v1/account/keys", headers=site(A, OPERATOR))[0] == 401)
check("missing X-Customer-Id -> 400",
      call("GET", "/v1/account/keys", headers={"Authorization": f"Bearer {SERVER}"})[0] == 400)
s, _ = call("POST", "/v1/account/keys", headers=site(A), body={"name": "x", "customer_id": B})
check("customer_id smuggled in body -> 422", s == 422, f"got {s}")

print("\n== create and list keys")
s, a1 = call("POST", "/v1/account/keys", headers=site(A), body={"name": "A primary"})
check("A creates key 1 -> 201, secret returned once", s == 201 and a1["secret"].startswith("uak_"), f"prefix {a1.get('prefix')}")
s, a2 = call("POST", "/v1/account/keys", headers=site(A), body={"name": "A secondary"})
check("A creates key 2 -> 201", s == 201, f"prefix {a2.get('prefix')}")
s, b1 = call("POST", "/v1/account/keys", headers=site(B), body={"name": "B primary"})
check("B creates key -> 201", s == 201, f"prefix {b1.get('prefix')}")
check("customer key cannot call account API -> 401", call("GET", "/v1/account/keys", headers=site(A, a1["secret"]))[0] == 401)
_, credits = call("GET", "/v1/account/credits", headers=site(A))
if credits and credits.get("billing_mode") == "on":  # credits are enforced: grant test credits
    assert OPERATOR, "billing is enforced: set API_KEY (operator) so the test can grant credits"
    for cust, n in ((A, 200), (B, 10)):
        s, _ = call("POST", "/v1/admin/credits/adjust", headers={"X-API-Key": OPERATOR},
                    body={"customer_id": cust, "delta": n, "note": "account smoke test"})
        check(f"granted {n} test credits to {cust[:12]}... (billing_mode={credits['billing_mode']})", s == 200)
_, la = call("GET", "/v1/account/keys", headers=site(A))
_, lb = call("GET", "/v1/account/keys", headers=site(B))
check("A lists exactly its 2 keys", sorted(k["id"] for k in la["keys"]) == sorted([a1["id"], a2["id"]]))
check("B lists exactly its 1 key", [k["id"] for k in lb["keys"]] == [b1["id"]])
check("listings carry metadata only (no secrets)",
      all(set(k) == {"id", "name", "prefix", "status", "created_at", "last_used_at", "revoked_at"} for k in la["keys"])
      and a1["secret"] not in json.dumps(la))

print("\n== isolation between customers")
check("B revoking A's key -> 404", call("POST", f"/v1/account/keys/{a1['id']}/revoke", headers=site(B))[0] == 404)
check("B reading usage of A's key -> 404", call("GET", f"/v1/account/usage?key_id={a1['id']}", headers=site(B))[0] == 404)

print("\n== all 10 operations with a customer key (A primary), one request")
s, body = execute(a1["secret"], ALL10)
check("POST /v1/execute with customer key -> 200", s == 200, f"got {s}")
for r in body["results"]:
    ok = r["status"] == "ok" and bool(smoke_test.CASES[r["operation"]][1](r["output"]))
    check(f"  {r['operation']:30s} status={r['status']}", ok, f"{r['duration_ms']} ms")
s, body = execute(a2["secret"], [FUZZ, {"id": "u", "operation": "nope.op", "inputs": {}}])
check("A secondary: 1 request, 2 tasks -> 200", s == 200)
s, body = execute(b1["secret"], [FUZZ])
check("B: 1 request, 1 task -> 200", s == 200 and body["results"][0]["status"] == "ok")

print("\n== usage")
_, ua = call("GET", "/v1/account/usage", headers=site(A))
_, ub = call("GET", "/v1/account/usage", headers=site(B))
check("A totals: 2 requests, 12 operations (11 ok, 1 error)",
      {k: ua["totals"][k] for k in ("requests", "operations", "operations_ok", "operations_error")}
      == {"requests": 2, "operations": 12, "operations_ok": 11, "operations_error": 1}, ua["totals"])
check("A by_operation covers all 10 operations + 'unknown'",
      {o["operation"] for o in ua["by_operation"]} == set(smoke_test.CASES) | {"unknown"})
byk = {k["key_id"]: k for k in ua["by_key"]}
check("A by_key: primary 1 req/10 ops, secondary 1 req/2 ops",
      (byk[a1["id"]]["requests"], byk[a1["id"]]["operations"], byk[a2["id"]]["requests"], byk[a2["id"]]["operations"]) == (1, 10, 1, 2))
check("B totals: 1 request, 1 operation, none of A's keys",
      ub["totals"]["requests"] == 1 and ub["totals"]["operations"] == 1 and {k["key_id"] for k in ub["by_key"]} == {b1["id"]})
dump = json.dumps([ua, ub])
check("usage holds no secrets, inputs or task ids",
      all(x not in dump for x in (a1["secret"], a2["secret"], b1["secret"], "Drift Protein", '"fuzz"')))
_, la = call("GET", "/v1/account/keys", headers=site(A))
check("last_used_at set after use", all(k["last_used_at"] for k in la["keys"]))

print("\n== revocation")
s, rk = call("POST", f"/v1/account/keys/{a2['id']}/revoke", headers=site(A))
check("A revokes key 2 -> 200, status revoked", s == 200 and rk["status"] == "revoked" and rk["revoked_at"])
s, _ = execute(a2["secret"], [FUZZ])
check("revoked key's next call -> 401", s == 401, f"got {s}")
check("A's other key still works -> 200", execute(a1["secret"], [FUZZ])[0] == 200)
check("B unaffected -> 200", execute(b1["secret"], [FUZZ])[0] == 200)
_, ua = call("GET", "/v1/account/usage", headers=site(A))
byk = {k["key_id"]: k for k in ua["by_key"]}
check("revoked key's usage history preserved", byk[a2["id"]]["status"] == "revoked" and byk[a2["id"]]["requests"] == 1
      and ua["totals"]["requests"] == 3)

print("\n== full smoke test with a customer key")
proc = subprocess.run([sys.executable, str(Path(__file__).with_name("smoke_test.py")), BASE],
                      env={**os.environ, "API_KEY": a1["secret"]}, capture_output=True, text=True)
print("\n".join("  " + l for l in proc.stdout.strip().splitlines()))
check("smoke_test.py with customer key", proc.returncode == 0, proc.stderr.strip()[-300:])

for cust, key in ((A, a1), (B, b1)):  # leave no active test keys behind (usage is kept)
    call("POST", f"/v1/account/keys/{key['id']}/revoke", headers=site(cust))

print(f"\n{'ALL ACCOUNT CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
