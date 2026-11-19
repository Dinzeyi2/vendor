"""DoWhy: estimate a causal effect from observational data.

Documented API: dowhy.CausalModel(data, treatment, outcome, common_causes, instruments)
  -> identify_effect() -> estimate_effect(estimand, method_name=...)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

import pandas as pd
from dowhy import CausalModel

from app.errors import OperationError
from app.ops._validate import choice, require, string

logging.getLogger("dowhy").setLevel(logging.WARNING)

METHODS = [
    "backdoor.linear_regression",
    "backdoor.propensity_score_matching",
    "backdoor.propensity_score_stratification",
    "backdoor.propensity_score_weighting",
    "iv.instrumental_variable",
]
MAX_ROWS = 200_000


def _names(inputs: Dict[str, Any], key: str, columns: List[str]) -> List[str]:
    value = inputs.get(key) or []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise OperationError(f"input '{key}' must be a list of column names")
    missing = [v for v in value if v not in columns]
    if missing:
        raise OperationError(f"input '{key}' names unknown columns: {missing}")
    return value


def estimate_effect(inputs: Dict[str, Any]) -> Dict[str, Any]:
    data = require(inputs, "data")
    if not isinstance(data, dict) or not data:
        raise OperationError("input 'data' must be an object mapping column name -> list of numbers")
    lengths = {len(v) if isinstance(v, list) else -1 for v in data.values()}
    if len(lengths) != 1 or -1 in lengths:
        raise OperationError("every column in 'data' must be a list of the same length")
    n_rows = lengths.pop()
    if not 10 <= n_rows <= MAX_ROWS:
        raise OperationError(f"'data' must have between 10 and {MAX_ROWS} rows")
    try:
        df = pd.DataFrame(data).astype(float)
    except (TypeError, ValueError):
        raise OperationError("all 'data' values must be numeric") from None
    columns = list(df.columns)

    treatment = string(inputs, "treatment")
    outcome = string(inputs, "outcome")
    for name in (treatment, outcome):
        if name not in columns:
            raise OperationError(f"column '{name}' is not in 'data'")
    common_causes = _names(inputs, "common_causes", columns)
    instruments = _names(inputs, "instruments", columns)
    method = choice(inputs, "method", "backdoor.linear_regression", METHODS)
    if method.startswith("backdoor.propensity"):
        if set(df[treatment].unique()) - {0.0, 1.0}:
            raise OperationError("propensity-score methods need a binary 0/1 treatment column")
        df[treatment] = df[treatment].astype(bool)

    model = CausalModel(data=df, treatment=treatment, outcome=outcome,
                        common_causes=common_causes or None, instruments=instruments or None)
    estimand = model.identify_effect(proceed_when_unidentifiable=True)
    estimate = model.estimate_effect(estimand, method_name=method, target_units="ate")
    if estimate.value is None:
        raise OperationError(f"effect is not identifiable with method '{method}' and the given variables")
    return {
        "effect": float(estimate.value),
        "method": method,
        "treatment": treatment,
        "outcome": outcome,
        "estimand_type": str(estimand.estimand_type),
        "n_rows": n_rows,
    }


OPERATIONS = {"dowhy.estimate_effect": estimate_effect}
