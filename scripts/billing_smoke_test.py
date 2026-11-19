"""Real HTTP test of prepaid credits (server must run with BILLING_MODE=on).

Usage:
  WEBSITE_BACKEND_SECRET=... CREDITS_GRANT_SECRET=... API_KEY=<operator key> \
      python scripts/billing_smoke_test.py BASE_URL

Credits are added exactly the way the website's server will: POST /v1/credits/grant with a
customer ID, an amount and a unique payment ID. Secrets are never printed.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

sys.path.insert(0, str(Path(__file__).resolve().parent))
import smoke_test  # noqa: E402  (10 known-answer cases; reads BASE from argv)

BASE = smoke_test.BASE
SERVER = os.environ["WEBSITE_BACKEND_SECRET"]
GRANT = os.environ["CREDITS_GRANT_SECRET"]
OPERATOR = os.environ["API_KEY"]
failures = 0


def call(method, path, headers=None, body=None):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def site(c):
    return {"Authorization": f"Bearer {SERVER}", "X-Customer-Id": c}


def check(label, ok, detail=""):
    global failures
    failures += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))


def balance(c):
    return call("GET", "/v1/account/credits", headers=site(c))[1]["balance"]


def key(c, name):
    s, k = call("POST", "/v1/account/keys", headers=site(c), body={"name": name})
    assert s == 201, (s, k)
    return k


def grant(c, credits, payment_id, secret=GRANT):
    return call("POST", "/v1/credits/grant", headers={"Authorization": f"Bearer {secret}"},
                body={"customer_id": c, "credits": credits, "payment_id": payment_id})


def pay(c, credits):
    s, r = grant(c, credits, "pay_smoke_" + secrets.token_hex(10))
    assert s == 201 and r["status"] == "credited", (s, r)


def execute(k, tasks):
    return call("POST", "/v1/execute", headers={"X-API-Key": k}, body={"tasks": tasks})


FUZZ = {"id": "f", "operation": "rapidfuzz.compare", "inputs": {"a": "Drift Protein", "b": "Drift Proteins"}}


def fuzz(n):
    return [{**FUZZ, "id": f"f{i}"} for i in range(n)]


run = secrets.token_hex(4)
A, B = f"billtest-a-{run}", f"billtest-b-{run}"
print(f"== {BASE}  customers {A}, {B}")
mode = call("GET", "/v1/account/credits", headers=site(A))[1].get("billing_mode")
print("billing_mode:", mode)
if mode != "on":
    print("server is not enforcing credits (BILLING_MODE is not 'on'); nothing to test")
    sys.exit(2)

print("\n== the grant endpoint only accepts its own secret")
for label, secret in (("website-backend secret", SERVER), ("operator key", OPERATOR), ("wrong secret", "x" * 40)):
    check(f"grant with {label} -> 401", grant(A, 5, "pay_forged_" + run, secret)[0] == 401)
check("nothing was credited", balance(A) == 0)

print("\n== zero credits blocks execution and runs nothing")
a1, a2, b1 = key(A, "A one"), key(A, "A two"), key(B, "B one")
check("new account with keys has balance 0", balance(A) == 0 and balance(B) == 0)
s, r = execute(a1["secret"], fuzz(1))
check("balance 0 -> 402 insufficient_credits", s == 402 and r.get("error") == "insufficient_credits", f"{s} {r}")
check("nothing ran: 0 usage requests", call("GET", "/v1/account/usage", headers=site(A))[1]["totals"]["requests"] == 0)

print("\n== a payment restores access, exactly once")
pid = "cs_test_smoke_" + secrets.token_hex(10)
s, r = grant(A, 3, pid)
check("website reports payment -> 201 credited, balance 3", s == 201 and r["status"] == "credited" and r["balance"] == 3)
s, r = grant(A, 3, pid)
check("same payment reported again -> 200 already_credited, balance still 3", s == 200 and r["status"] == "already_credited" and balance(A) == 3)
barrier = Barrier(20)


def race(_):
    barrier.wait()
    return grant(A, 3, pid)[1].get("status")


with ThreadPoolExecutor(20) as pool:
    statuses = list(pool.map(race, range(20)))
check("20 simultaneous re-reports of that payment add nothing", set(statuses) == {"already_credited"} and balance(A) == 3)
check("same payment_id with another amount -> 409", grant(A, 300, pid)[0] == 409 and balance(A) == 3)
check("same payment_id for another customer -> 409", grant(B, 3, pid)[0] == 409 and balance(B) == 0)
s, r = execute(a1["secret"], fuzz(1))
check("access restored: customer key -> 200", s == 200 and r["results"][0]["status"] == "ok")

print("\n== multiple keys share one balance; key changes never touch it")
check("key two spends the remaining 2 -> 200", execute(a2["secret"], fuzz(2))[0] == 200 and balance(A) == 0)
check("both keys blocked at 0 (402)", execute(a1["secret"], fuzz(1))[0] == 402 and execute(a2["secret"], fuzz(1))[0] == 402)
pay(A, 4)
call("POST", f"/v1/account/keys/{a2['id']}/revoke", headers=site(A))
a2 = key(A, "A three")
check("revoking and creating keys leaves the balance at 4", balance(A) == 4)

print("\n== two customers stay isolated")
check("B still 0 and blocked while A has 4", balance(B) == 0 and execute(b1["secret"], fuzz(1))[0] == 402 and balance(A) == 4)

print("\n== simultaneous calls cannot overspend (40 parallel requests x 2 credits, balance 5)")
pay(A, 1)
barrier = Barrier(40)


def fire(i):
    barrier.wait()
    return execute((a1, a2)[i % 2]["secret"], fuzz(2))[0]


with ThreadPoolExecutor(40) as pool:
    codes = list(pool.map(fire, range(40)))
check("exactly 2 succeed, 38 get 402", codes.count(200) == 2 and codes.count(402) == 38,
      f"200 x{codes.count(200)}, 402 x{codes.count(402)}, other {[c for c in codes if c not in (200, 402)]}")
check("balance ends at 1, never negative", balance(A) == 1)

print("\n== all 10 operations with credits")
pay(A, 9)                                          # 10
s, r = execute(a1["secret"], [{"id": op.split(".")[0], "operation": op, "inputs": inp}
                              for op, (inp, _, _) in smoke_test.CASES.items()])
ok = s == 200 and all(t["status"] == "ok" and smoke_test.CASES[t["operation"]][1](t["output"]) for t in r["results"])
check("one request, 10 operations -> all ok with correct answers", ok,
      ", ".join(f"{t['operation'].split('.')[0]}={t['status']}" for t in r.get("results", [])))
check("charged exactly 10 credits (balance 0)", balance(A) == 0)
u = call("GET", "/v1/account/usage", headers=site(A))[1]
check("usage recorded with credits_charged", u["recent_requests"][0]["credits_charged"] == 10)
check("operator key still works and is not charged", execute(OPERATOR, fuzz(3))[0] == 200)

for c, k in ((A, a1), (A, a2), (B, b1)):
    call("POST", f"/v1/account/keys/{k['id']}/revoke", headers=site(c))
print(f"\n{'ALL BILLING CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
