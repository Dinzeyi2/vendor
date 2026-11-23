"""Real HTTP smoke test for every operation on a running server.

Usage:  API_KEY=... python scripts/smoke_test.py [BASE_URL]      (default http://127.0.0.1:8000)

Each check sends a real POST /v1/execute and verifies the answer against a known result.
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request

import numpy as np
from PIL import Image, ImageDraw, ImageFont

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000").rstrip("/")


KEY = os.environ.get("API_KEY", "")


def post(tasks, timeout=1200, key=None):
    req = urllib.request.Request(f"{BASE}/v1/execute", data=json.dumps({"tasks": tasks}).encode(),
                                 headers={"Content-Type": "application/json", "X-API-Key": key or KEY}, method="POST")
    t = time.perf_counter()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read())
    return body["results"], time.perf_counter() - t


def png_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def font(size):
    for name in ("arial.ttf", "DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


rng = np.random.default_rng(42)

# --- inputs -----------------------------------------------------------------
x_lin = np.linspace(0, 5, 60)
w = rng.normal(size=600)
t = 0.5 * w + rng.normal(size=600)
y_causal = 3.0 * t + 2.0 * w + rng.normal(scale=0.5, size=600)
square = Image.new("RGB", (64, 64), "black")
ImageDraw.Draw(square).rectangle([16, 16, 47, 47], fill="white")
text_img = Image.new("RGB", (520, 120), "white")
ImageDraw.Draw(text_img).text((30, 30), "Drift Protein", fill="black", font=font(56))
x_sr = np.linspace(-2, 2, 40)

CASES = {
    "text.similarity": (
        {"a": "Drift Protein", "b": "Drift Proteins"},
        lambda o: abs(o["score"] - 96.296) < 0.01, "score ~ 96.30"),
    "text.rank_matches": (
        {"query": "Jon Smith", "choices": ["Jane Doe", "John Smith", "Jonathan Smythe"]},
        lambda o: o["matches"][0]["choice"] == "John Smith", "best match for 'Jon Smith' is 'John Smith'"),
    "vectors.nearest_neighbors": (
        {"vectors": [[0, 0], [1, 0], [0, 1], [5, 5]], "queries": [[0.9, 0.1]], "k": 2, "space": "l2"},
        lambda o: o["labels"][0] == [1, 0], "nearest to (0.9,0.1) are ids [1, 0]"),
    "tabular.train_and_predict": (
        {"X": [[v] for v in x_lin], "y": list(2 * x_lin + 1), "X_predict": [[2.5]], "n_estimators": 200},
        lambda o: abs(o["predictions"][0] - 6.0) < 0.3, "learns y=2x+1, predicts f(2.5) ~ 6"),
    "optimization.solve_convex": (
        {"c": [-1, -1], "A": [[1, 2], [3, 1], [-1, 0], [0, -1]], "b": [4, 6, 0, 0], "cone": {"l": 4}},
        lambda o: o["status"] == "solved" and abs(o["objective"] + 2.8) < 1e-4
        and abs(o["x"][0] - 1.6) < 1e-4 and abs(o["x"][1] - 1.2) < 1e-4, "LP optimum x=(1.6,1.2), obj=-2.8"),
    "causal.estimate_effect": (
        {"data": {"t": t.tolist(), "y": y_causal.tolist(), "w": w.tolist()},
         "treatment": "t", "outcome": "y", "common_causes": ["w"]},
        lambda o: abs(o["effect"] - 3.0) < 0.15, "recovers true effect 3.0 (confounded by w)"),
    "image.detect_edges": (
        {"image_base64": png_b64(square)},
        lambda o: 80 <= o["edge_pixels"] <= 200 and o["edges_png_base64"], "edges of a 32x32 square (~124 px)"),
    "image.extract_text": (
        {"image_base64": png_b64(text_img), "lang": "en"},
        lambda o: "driftprotein" in o["text"].replace(" ", "").lower(), "reads 'Drift Protein' from rendered PNG"),
    "math.discover_equation": (
        {"X": [[v] for v in x_sr], "y": list(x_sr ** 2 + 2), "niterations": 15, "random_state": 0},
        lambda o: o["best"]["loss"] < 1e-6, "rediscovers y = x0^2 + 2"),
    "scheduling.school_timetable": (
        {"timeslots": [{"id": f"ts{i}", "day_of_week": "MONDAY", "start_time": s, "end_time": e}
                       for i, (s, e) in enumerate([("08:30", "09:30"), ("09:30", "10:30"), ("10:30", "11:30")])],
         "rooms": [{"id": "rA", "name": "Room A"}, {"id": "rB", "name": "Room B"}],
         "lessons": [
             {"id": "l1", "subject": "Math", "teacher": "A. Turing", "student_group": "9th"},
             {"id": "l2", "subject": "Physics", "teacher": "M. Curie", "student_group": "9th"},
             {"id": "l3", "subject": "Chemistry", "teacher": "M. Curie", "student_group": "10th"},
             {"id": "l4", "subject": "Math", "teacher": "A. Turing", "student_group": "10th"},
             {"id": "l5", "subject": "History", "teacher": "I. Jones", "student_group": "9th"},
             {"id": "l6", "subject": "Biology", "teacher": "C. Darwin", "student_group": "10th"}],
         "time_limit_seconds": 5},
        lambda o: o["feasible"] and all(a["timeslot_id"] and a["room_id"] for a in o["assignments"]),
        "6 lessons scheduled with 0 hard-constraint violations"),
    "physics.simulate_rigid_bodies": (
        {"bodies": [
            {"id": "ground", "type": "fixed", "position": [0, 0, 0], "shape": {"type": "cuboid", "half_extents": [10, 0.1, 10]}},
            {"id": "ball", "type": "dynamic", "position": [0, 5, 0], "shape": {"type": "ball", "radius": 0.5}}],
         "steps": 240},
        lambda o: abs(next(b for b in o["bodies"] if b["id"] == "ball")["position"][1] - 0.6) < 0.05,
        "ball dropped from y=5 rests on ground at y~0.6"),
}


# Original names, kept as compatibility aliases.
OLD_NAMES = {
    "rapidfuzz.compare": "text.similarity",
    "hnswlib.knn_query": "vectors.nearest_neighbors",
    "xgboost.train_predict": "tabular.train_and_predict",
    "scs.solve": "optimization.solve_convex",
    "dowhy.estimate_effect": "causal.estimate_effect",
    "kornia.canny": "image.detect_edges",
    "paddleocr.ocr": "image.extract_text",
    "pysr.symbolic_regression": "math.discover_equation",
    "timefold.school_timetabling": "scheduling.school_timetable",
    "rapier.simulate": "physics.simulate_rigid_bodies",
}


def summarize(op, out):
    if op == "text.similarity": return f"score={out['score']:.4f}"
    if op == "text.rank_matches": return f"matches={[(m['choice'], m['score']) for m in out['matches']]}"
    if op == "vectors.nearest_neighbors": return f"labels={out['labels']} distances={[[round(d, 3) for d in r] for r in out['distances']]}"
    if op == "tabular.train_and_predict": return f"prediction(2.5)={out['predictions'][0]:.4f}"
    if op == "optimization.solve_convex": return f"status={out['status']} x={[round(v, 5) for v in out['x']]} obj={out['objective']:.5f}"
    if op == "causal.estimate_effect": return f"effect={out['effect']:.4f} ({out['method']})"
    if op == "image.detect_edges": return f"edge_pixels={out['edge_pixels']} of {out['width']}x{out['height']}"
    if op == "image.extract_text": return f"text={out['text']!r} conf={[round(l['confidence'], 3) for l in out['lines']]}"
    if op == "math.discover_equation": return f"best={out['best']['equation']!r} loss={out['best']['loss']:.2e}"
    if op == "scheduling.school_timetable": return f"score={out['score']} feasible={out['feasible']}"
    if op == "physics.simulate_rigid_bodies":
        ball = next(b for b in out["bodies"] if b["id"] == "ball")
        return f"ball y={ball['position'][1]:.4f} after {out['simulated_time']:.1f}s"
    return ""


def main() -> None:
    global KEY
    KEY = os.environ["API_KEY"]
    failures = 0
    print(f"== individual operations against {BASE}")
    for op, (inputs, check, expect) in CASES.items():
        (res,), secs = post([{"id": op, "operation": op, "inputs": inputs}])
        ok = res["status"] == "ok" and bool(check(res["output"]))
        failures += not ok
        detail = summarize(op, res["output"]) if res["status"] == "ok" else f"{res['error_type']}: {res['error']}"
        print(f"[{'PASS' if ok else 'FAIL'}] {op:30s} {secs:6.2f}s  expect: {expect}\n         got: {detail}")

    print("\n== one request calling all 10 operations")
    results, secs = post([{"id": op, "operation": op, "inputs": inp} for op, (inp, _, _) in CASES.items()])
    for r in results:
        ok = r["status"] == "ok" and bool(CASES[r["operation"]][1](r["output"]))
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {r['id']:30s} {r['status']:5s} {r['duration_ms']:9.1f} ms")
    print(f"total request time {secs:.2f}s")

    print("\n== plain-English requests: Cognex picks the operation")
    routed = [
        ({"id": "compare", "request": "Compare these two names", "data": ["Drift Protein", "Drift Proteins"]},
         "text.similarity", "ok"),
        ({"id": "closest", "request": "Find the closest names in this list",
          "data": {"name": "Jon Smith", "list": ["Jane Doe", "John Smith", "Jonathan Smythe"]}}, "text.rank_matches", "ok"),
        ({"id": "ask", "request": "Compare these two names"}, "text.similarity", "needs_clarification"),
    ]
    results, secs = post([t for t, _, _ in routed])
    for r, (_, op, status) in zip(results, routed):
        ok = r["status"] == status and r.get("operation") == op
        if ok and status == "ok":
            ok = bool(CASES[op][1](r["output"]))
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {r['id']:10s} -> {r.get('operation')} {r['status']} {r.get('question') or ''}")

    print("\n== compatibility: the original names still work and are echoed back")
    results, secs = post([{"id": old, "operation": old, "inputs": CASES[new][0]} for old, new in OLD_NAMES.items()])
    for r in results:
        ok = (r["status"] == "ok" and r["operation"] == r["id"]
              and bool(CASES[OLD_NAMES[r["operation"]]][1](r["output"])))
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {r['id']:30s} -> {OLD_NAMES[r['id']]}")

    print("\n== failure isolation: bad tasks next to good ones")
    slow_xgb = {"X": rng.normal(size=(20000, 50)).tolist(), "y": rng.normal(size=20000).tolist(),
                "X_predict": [[0.0] * 50], "n_estimators": 2000, "max_depth": 12}
    results, secs = post([
        {"id": "slow", "operation": "tabular.train_and_predict", "inputs": slow_xgb, "timeout_s": 2},
        {"id": "bad-input", "operation": "optimization.solve_convex", "inputs": {"c": [1], "A": [[1, 2]], "b": [1], "cone": {"l": 1}}},
        {"id": "unknown", "operation": "nope.op", "inputs": {}},
        {"id": "fine", "operation": "text.similarity", "inputs": {"a": "Drift Protein", "b": "Drift Proteins"}},
        {"id": "fine-2", "operation": "physics.simulate_rigid_bodies", "inputs": CASES["physics.simulate_rigid_bodies"][0]},
    ])
    expected = {"slow": "timeout", "bad-input": "invalid_input", "unknown": "unknown_operation", "fine": None, "fine-2": None}
    for r in results:
        want = expected[r["id"]]
        ok = (r["status"] == "ok") if want is None else (r.get("error_type") == want)
        failures += not ok
        print(f"[{'PASS' if ok else 'FAIL'}] {r['id']:10s} {r['status']:5s} {r.get('error_type') or '':18s} {(r.get('error') or '')[:70]}")
    (res,), secs = post([{"id": "after", "operation": "tabular.train_and_predict", "inputs": CASES["tabular.train_and_predict"][0]}])
    ok = res["status"] == "ok"
    failures += not ok
    print(f"[{'PASS' if ok else 'FAIL'}] xgboost worker restarted after timeout and answered in {secs:.2f}s")

    print(f"\n{'ALL CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
