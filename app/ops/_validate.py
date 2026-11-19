"""Small input validators shared by operations. All raise OperationError (-> invalid_input)."""
from __future__ import annotations

import base64
import binascii
import io
import math
from typing import Any, Dict, List, Optional, Sequence

from app.errors import OperationError

MAX_IMAGE_PIXELS = 16_000_000  # ~4000x4000


def require(inputs: Dict[str, Any], key: str) -> Any:
    if key not in inputs or inputs[key] is None:
        raise OperationError(f"missing required input '{key}'")
    return inputs[key]


def string(inputs: Dict[str, Any], key: str) -> str:
    value = require(inputs, key)
    if not isinstance(value, str):
        raise OperationError(f"input '{key}' must be a string")
    return value


def number(inputs: Dict[str, Any], key: str, default: float, lo: float, hi: float) -> float:
    value = inputs.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise OperationError(f"input '{key}' must be a finite number")
    if not lo <= value <= hi:
        raise OperationError(f"input '{key}' must be between {lo} and {hi}")
    return float(value)


def integer(inputs: Dict[str, Any], key: str, default: Optional[int], lo: int, hi: int) -> int:
    value = inputs.get(key, default)
    if value is None:
        raise OperationError(f"missing required input '{key}'")
    if isinstance(value, bool) or not isinstance(value, int):
        raise OperationError(f"input '{key}' must be an integer")
    if not lo <= value <= hi:
        raise OperationError(f"input '{key}' must be between {lo} and {hi}")
    return value


def vector(value: Any, name: str, length: Optional[int] = None) -> List[float]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        raise OperationError(f"input '{name}' must be a list of numbers")
    out = []
    for v in value:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise OperationError(f"input '{name}' must contain only finite numbers")
        out.append(float(v))
    if length is not None and len(out) != length:
        raise OperationError(f"input '{name}' must have length {length}")
    return out


def matrix(value: Any, name: str, max_rows: int = 100_000, max_cols: int = 10_000,
           cols: Optional[int] = None, allow_empty: bool = False):
    """2-D list of finite numbers -> float64 numpy array (rows x cols)."""
    import numpy as np

    if not isinstance(value, Sequence) or isinstance(value, str):
        raise OperationError(f"input '{name}' must be a 2-D list of numbers")
    if not value and not allow_empty:
        raise OperationError(f"input '{name}' must not be empty")
    if len(value) > max_rows:
        raise OperationError(f"input '{name}' has more than {max_rows} rows")
    rows = [vector(r, f"{name}[{i}]") for i, r in enumerate(value)]
    width = cols if cols is not None else (len(rows[0]) if rows else 0)
    if width == 0 and rows:
        raise OperationError(f"input '{name}' rows must not be empty")
    if width > max_cols:
        raise OperationError(f"input '{name}' has more than {max_cols} columns")
    if any(len(r) != width for r in rows):
        raise OperationError(f"every row of '{name}' must have {width} values")
    return np.asarray(rows, dtype=np.float64).reshape(len(rows), width)


def image(inputs: Dict[str, Any], key: str = "image_base64"):
    """Base64-encoded PNG/JPEG/... -> RGB PIL image."""
    from PIL import Image, UnidentifiedImageError

    data = string(inputs, key)
    if "," in data and data.startswith("data:"):
        data = data.split(",", 1)[1]
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        raise OperationError(f"input '{key}' is not valid base64") from None
    try:
        img = Image.open(io.BytesIO(raw))
        if img.width * img.height > MAX_IMAGE_PIXELS:
            raise OperationError(f"image is larger than {MAX_IMAGE_PIXELS} pixels")
        return img.convert("RGB")
    except UnidentifiedImageError:
        raise OperationError(f"input '{key}' is not a supported image format") from None


def choice(inputs: Dict[str, Any], key: str, default: str, allowed: Sequence[str]) -> str:
    value = inputs.get(key, default)
    if value not in allowed:
        raise OperationError(f"input '{key}' must be one of {list(allowed)}")
    return value
