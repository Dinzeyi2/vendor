"""SCS: convex cone program solver.

Solves  minimize ½xᵀPx + cᵀx  subject to  Ax + s = b,  s ∈ K
Documented API: scs.SCS(data={P, A, b, c}, cone={z, l, q, s, ep, ed, p, bl, bu}, **settings).solve()
Rows of A must be ordered by cone: zero (z), linear (l), box, second-order (q), PSD (s), exp, power.
"""
from __future__ import annotations

from typing import Any, Dict

import numpy as np
import scipy.sparse as sp
import scs

from app.errors import OperationError
from app.ops._validate import integer, matrix, number, require, vector

_INT_CONES = ("z", "l", "ep", "ed")
_LIST_CONES = ("q", "s")


def solve(inputs: Dict[str, Any]) -> Dict[str, Any]:
    c = np.asarray(vector(require(inputs, "c"), "c"))
    n = len(c)
    if n == 0:
        raise OperationError("input 'c' must not be empty")
    A = matrix(require(inputs, "A"), "A", max_rows=50_000, cols=n)
    b = np.asarray(vector(require(inputs, "b"), "b", length=A.shape[0]))

    raw_cone = require(inputs, "cone")
    if not isinstance(raw_cone, dict) or not raw_cone:
        raise OperationError("input 'cone' must be an object such as {\"l\": 3}")
    cone: Dict[str, Any] = {}
    for key, value in raw_cone.items():
        if key in _INT_CONES:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise OperationError(f"cone '{key}' must be a non-negative integer")
            cone[key] = value
        elif key in _LIST_CONES:
            if not isinstance(value, list) or any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in value):
                raise OperationError(f"cone '{key}' must be a list of positive integers")
            cone[key] = value
        else:
            raise OperationError(f"unsupported cone '{key}' (supported: z, l, q, s, ep, ed)")
    rows = (cone.get("z", 0) + cone.get("l", 0) + sum(cone.get("q", []))
            + sum(d * (d + 1) // 2 for d in cone.get("s", [])) + 3 * cone.get("ep", 0) + 3 * cone.get("ed", 0))
    if rows != A.shape[0]:
        raise OperationError(f"cone dimensions add up to {rows} rows but A has {A.shape[0]}")

    data: Dict[str, Any] = {"A": sp.csc_matrix(A), "b": b, "c": c}
    if inputs.get("P") is not None:
        P = matrix(inputs["P"], "P", max_rows=n, cols=n)
        if P.shape[0] != n or not np.allclose(P, P.T):
            raise OperationError("input 'P' must be a symmetric n x n matrix")
        data["P"] = sp.triu(sp.csc_matrix(P), format="csc")

    solver = scs.SCS(data, cone, verbose=False,
                     eps_abs=number(inputs, "eps_abs", 1e-6, 1e-12, 1.0),
                     eps_rel=number(inputs, "eps_rel", 1e-6, 1e-12, 1.0),
                     max_iters=integer(inputs, "max_iters", 100_000, 1, 10_000_000))
    sol = solver.solve()
    info = sol["info"]
    return {
        "status": info["status"], "status_val": int(info["status_val"]),
        "x": sol["x"].tolist(), "y": sol["y"].tolist(), "s": sol["s"].tolist(),
        "objective": float(info["pobj"]), "dual_objective": float(info["dobj"]),
        "iterations": int(info["iter"]), "solve_time_ms": float(info["solve_time"]),
    }


OPERATIONS = {"scs.solve": solve}
