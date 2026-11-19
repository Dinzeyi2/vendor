"""PySR: symbolic regression (Julia backend via juliacall).

Documented API: pysr.PySRRegressor(niterations, binary_operators, unary_operators, ...).fit(X, y)
Operators are restricted to an allowlist: PySR evaluates operator strings as Julia code.
"""
from __future__ import annotations

from typing import Any, Dict

import numpy as np
from pysr import PySRRegressor

from app.errors import OperationError
from app.ops._validate import integer, matrix, require, vector

BINARY = ["+", "-", "*", "/", "^", "max", "min"]
UNARY = ["sin", "cos", "exp", "log", "sqrt", "abs", "square", "cube", "neg", "inv", "tanh"]


def _ops(inputs: Dict[str, Any], key: str, default, allowed) -> list:
    value = inputs.get(key, default)
    if not isinstance(value, list) or any(v not in allowed for v in value):
        raise OperationError(f"input '{key}' must be a list drawn from {allowed}")
    return value


def symbolic_regression(inputs: Dict[str, Any]) -> Dict[str, Any]:
    X = matrix(require(inputs, "X"), "X", max_rows=10_000, max_cols=20)
    y = np.asarray(vector(require(inputs, "y"), "y", length=X.shape[0]))
    if X.shape[0] < 5:
        raise OperationError("need at least 5 rows")
    binary = _ops(inputs, "binary_operators", ["+", "-", "*", "/"], BINARY)
    unary = _ops(inputs, "unary_operators", [], UNARY)
    if not binary:
        raise OperationError("'binary_operators' must not be empty")
    seed = integer(inputs, "random_state", 0, 0, 2**31 - 1)

    model = PySRRegressor(
        niterations=integer(inputs, "niterations", 20, 1, 500),
        populations=integer(inputs, "populations", 8, 1, 64),
        maxsize=integer(inputs, "maxsize", 20, 5, 50),
        binary_operators=binary, unary_operators=unary,
        random_state=seed, deterministic=True, parallelism="serial",
        progress=False, verbosity=0, temp_equation_file=True, delete_tempfiles=True,
    )
    model.fit(X, y)
    best = model.get_best()
    table = model.equations_
    return {
        "best": {"equation": str(best["sympy_format"]), "julia": best["equation"],
                 "loss": float(best["loss"]), "complexity": int(best["complexity"])},
        "pareto_front": [
            {"equation": str(r["sympy_format"]), "loss": float(r["loss"]), "complexity": int(r["complexity"])}
            for _, r in table.iterrows()
        ],
        "feature_names": [f"x{i}" for i in range(X.shape[1])],
    }


OPERATIONS = {"pysr.symbolic_regression": symbolic_regression}
