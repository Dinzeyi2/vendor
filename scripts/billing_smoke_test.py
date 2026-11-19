"""Real HTTP test of prepaid credits (server must run with BILLING_MODE=test).

Usage:
  WEBSITE_BACKEND_SECRET=... API_KEY=<operator key> python scripts/billing_smoke_test.py BASE_URL
  ... --stripe PACK_ID   also run a real Stripe test-mode purchase: prints a Checkout URL to pay
                         with card 4242 4242 4242 4242, waits for the webhook to credit it, then
                         replays the real paid event 10x concurrently (needs STRIPE_SECRET_KEY and
                         STRIPE_WEBHOOK_SECRET in the environment) and checks it credited once.

Without --stripe, credits are granted with the operator-only adjustment endpoint.
Secrets are never printed.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

sys.path.insert(0, str(Path(__file__).resolve().parent))

parser = argparse.ArgumentParser()
parser.add_argument("base")
parser.add_argument("--stripe", metavar="PACK_ID")
args = parser.parse_args()
sys.argv = [sys.argv[0], args.base]
import smoke_test  # noqa: E402  (10 known-answer cases)

BASE = args.base.rstrip("/")
SERVER = os.environ["WEBSITE_BACKEND_SECRET"]
OPERATOR = os.environ["API_KEY"]
failures = 0


def call(method, path, headers=None, body=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, method=method, data=data,
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


def grant(c, n):
    s, r = call("POST", "/v1/admin/credits/adjust", headers={"X-API-Key": OPERATOR},
                body={"customer_id": c, "delta": n, "note": "billing smoke test"})
    assert s == 200, (s, r)


def execute(k, tasks):
    return call("POST", "/v1/execute", headers={"X-API-Key": k}, body={"tasks": tasks})


FUZZ = [{"id": "f", "operation": "rapidfuzz.compare", "inputs": {"a": "Drift Protein", "b": "Drift Proteins"}}]


def fuzz(n):
    return [{**FUZZ[0], "id": f"f{i}"} for i in range(n)]


run = secrets.token_hex(4)
A, B = f"billtest-a-{run}", f"billtest-b-{run}"
print(f"== {BASE}  customers {A}, {B}")
_, credits = call("GET", "/v1/account/credits", headers=site(A))
print("billing_mode:", credits.get("billing_mode"), "| packs:", credits.get("packs"))
if credits.get("billing_mode") not in ("test", "live"):
    print("server is not enforcing credits (BILLING_MODE=off); nothing to test")
    sys.exit(2)

print("\n== new accounts, keys and a shared balance")
check("new account balance is 0", balance(A) == 0)
a1, a2, b1 = key(A, "A one"), key(A, "A two"), key(B, "B one")
check("creating keys leaves the balance at 0", balance(A) == 0 and balance(B) == 0)

print("\n== insufficient credits blocks execution and runs nothing")
s, r = execute(a1["secret"], fuzz(1))
check("balance 0 -> 402 insufficient_credits", s == 402 and r.get("error") == "insufficient_credits", f"{s} {r}")
grant(A, 3)
s, r = execute(a1["secret"], fuzz(4))
check("batch of 4 with balance 3 -> 402, required 4", s == 402 and r.get("required") == 4 and r.get("balance") == 3)
_, u = call("GET", "/v1/account/usage", headers=site(A))
check("nothing ran: 0 usage requests, balance still 3", u["totals"]["requests"] == 0 and balance(A) == 3)

print("\n== multiple keys share one balance")
check("key one spends 2 -> 200", execute(a1["secret"], fuzz(2))[0] == 200)
check("key two sees 1 left and spends it -> 200", balance(A) == 1 and execute(a2["secret"], fuzz(1))[0] == 200)
check("both keys now blocked (402)", execute(a1["secret"], fuzz(1))[0] == 402 and execute(a2["secret"], fuzz(1))[0] == 402)
s, _ = call("POST", f"/v1/account/keys/{a2['id']}/revoke", headers=site(A))
check("revoking a key leaves the balance unchanged", s == 200 and balance(A) == 0)
a2 = key(A, "A three")
check("creating another key leaves the balance unchanged", balance(A) == 0)

print("\n== two customers stay isolated")
grant(A, 5)
check("A has 5, B has 0", (balance(A), balance(B)) == (5, 0))
check("B is blocked (402) while A has credits", execute(b1["secret"], fuzz(1))[0] == 402)
check("A's balance untouched by B's attempt", balance(A) == 5)

print("\n== simultaneous calls cannot overspend (40 parallel HTTP requests x 2 credits, balance 5)")
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
grant(A, 9)                                       # 10
s, r = execute(a1["secret"], [{"id": op.split(".")[0], "operation": op, "inputs": inp}
                              for op, (inp, _, _) in smoke_test.CASES.items()])
ok = s == 200 and all(t["status"] == "ok" and smoke_test.CASES[t["operation"]][1](t["output"]) for t in r["results"])
check("one request, 10 operations -> all ok with correct answers", ok,
      ", ".join(f"{t['operation'].split('.')[0]}={t['status']}" for t in r.get("results", [])))
check("charged exactly 10 credits (balance 0)", balance(A) == 0)
_, u = call("GET", "/v1/account/usage", headers=site(A))
check("usage still recorded, with credits_charged", u["totals"]["requests"] >= 4 and
      u["recent_requests"][0]["credits_charged"] == 10 and len(u["recent_requests"][0]["operations"]) == 10)
s, r = call("POST", "/v1/execute", headers={"X-API-Key": OPERATOR}, body={"tasks": fuzz(3)})
check("operator key still works and is not charged", s == 200)

if args.stripe:
    import stripe
    print(f"\n== real Stripe test-mode purchase (pack {args.stripe})")
    sk, whsec = os.environ["STRIPE_SECRET_KEY"], os.environ["STRIPE_WEBHOOK_SECRET"]
    assert sk.startswith(("sk_test_", "rk_test_")), "use a Stripe TEST secret key"
    before = balance(B)
    s, co = call("POST", "/v1/account/credits/checkout", headers=site(B), body={"pack_id": args.stripe})
    check("checkout session created -> 201", s == 201 and co["checkout_url"].startswith("https://checkout.stripe.com/"), s)
    check("creating the session adds nothing", balance(B) == before)
    print(f"\n  Pay with test card 4242 4242 4242 4242 (any future date, any CVC):\n  {co['checkout_url']}\n")
    deadline = time.time() + 900
    while time.time() < deadline and balance(B) == before:
        time.sleep(5)
    check(f"webhook credited the purchase (+{co['credits']})", balance(B) == before + co["credits"])
    check("payment restored access for B -> 200", execute(b1["secret"], fuzz(1))[0] == 200)
    after = balance(B)
    events = stripe.Event.list(api_key=sk, type="checkout.session.completed", limit=20)
    event = next(e for e in events.data if e.data.object.id == co["checkout_session_id"])
    payload = json.dumps(event.to_dict_recursive() if hasattr(event, "to_dict_recursive") else event.to_dict()).encode()

    def replay(_):
        t = int(time.time())
        sig = hmac.new(whsec.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
        return call("POST", "/v1/stripe/webhook", headers={"Stripe-Signature": f"t={t},v1={sig}"}, raw=payload)[1]

    with ThreadPoolExecutor(10) as pool:
        results = [r.get("result") for r in pool.map(replay, range(10))]
    check("10 concurrent redeliveries of the real event credit nothing extra",
          balance(B) == after and "credited" not in results, f"results {sorted(set(results))}")

for c, k in ((A, a1), (A, a2), (B, b1)):
    call("POST", f"/v1/account/keys/{k['id']}/revoke", headers=site(c))
print(f"\n{'ALL BILLING CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
