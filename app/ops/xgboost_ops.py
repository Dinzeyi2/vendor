"""XGBoost: train a gradient-boosted tree model on the given rows and predict new rows.

Documented API: xgboost.XGBRegressor / xgboost.XGBClassifier (scikit-learn interface).
"""
from __future__ import annotations

from typing import Any, Dict

import numpy as np
import xgboost as xgb

from app.errors import OperationError
from app.ops._validate import choice, integer, matrix, number, require, vector


def train_predict(inputs: Dict[str, Any]) -> Dict[str, Any]:
    X = matrix(require(inputs, "X"), "X", max_rows=200_000, max_cols=2000)
    y = np.asarray(vector(require(inputs, "y"), "y", length=X.shape[0]))
    X_pred = matrix(require(inputs, "X_predict"), "X_predict", max_rows=200_000, cols=X.shape[1])
    task = choice(inputs, "task", "regression", ["regression", "classification"])
    params = dict(
        n_estimators=integer(inputs, "n_estimators", 100, 1, 2000),
        max_depth=integer(inputs, "max_depth", 3, 1, 16),
        learning_rate=number(inputs, "learning_rate", 0.1, 1e-4, 1.0),
        random_state=integer(inputs, "random_state", 0, 0, 2**31 - 1),
        n_jobs=1,
    )
    out: Dict[str, Any] = {"task": task, "n_train": int(X.shape[0]), "n_features": int(X.shape[1])}
    if task == "regression":
        model = xgb.XGBRegressor(objective="reg:squarederror", **params)
        model.fit(X, y)
        out["predictions"] = model.predict(X_pred).tolist()
    else:
        if np.any(y != np.round(y)) or np.any(y < 0):
            raise OperationError("classification labels in 'y' must be non-negative integers 0..k-1")
        classes = np.unique(y.astype(int))
        if len(classes) < 2 or not np.array_equal(classes, np.arange(len(classes))):
            raise OperationError("classification labels in 'y' must be 0..k-1 with at least two classes")
        model = xgb.XGBClassifier(**params)
        model.fit(X, y.astype(int))
        out["predictions"] = model.predict(X_pred).astype(int).tolist()
        out["probabilities"] = model.predict_proba(X_pred).tolist()
    out["feature_importances"] = model.feature_importances_.tolist()
    return out


OPERATIONS = {"xgboost.train_predict": train_predict}
