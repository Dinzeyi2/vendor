"""Kornia: Canny edge detection on an image (PyTorch, CPU).

Documented API: kornia.filters.canny(input[B,C,H,W], low_threshold, high_threshold,
kernel_size, sigma) -> (magnitude, edges)
"""
from __future__ import annotations

import base64
import io
import os
from typing import Any, Dict

import numpy as np
import torch
import kornia
from PIL import Image

from app.errors import OperationError
from app.ops._validate import image, integer, number

torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))


def canny(inputs: Dict[str, Any]) -> Dict[str, Any]:
    img = image(inputs)
    low = number(inputs, "low_threshold", 0.1, 0.0, 1.0)
    high = number(inputs, "high_threshold", 0.2, 0.0, 1.0)
    if low > high:
        raise OperationError("'low_threshold' must be <= 'high_threshold'")
    kernel = integer(inputs, "kernel_size", 5, 3, 31)
    if kernel % 2 == 0:
        raise OperationError("'kernel_size' must be odd")
    sigma = number(inputs, "sigma", 1.0, 0.1, 20.0)

    arr = np.asarray(img, dtype=np.float32) / 255.0                  # H, W, 3
    tensor = torch.from_numpy(arr).permute(2, 0, 1).unsqueeze(0)       # 1, 3, H, W
    with torch.inference_mode():
        _magnitude, edges = kornia.filters.canny(tensor, low_threshold=low, high_threshold=high,
                                                 kernel_size=(kernel, kernel), sigma=(sigma, sigma))
    mask = edges[0, 0].numpy() > 0.5
    out: Dict[str, Any] = {
        "width": img.width, "height": img.height,
        "edge_pixels": int(mask.sum()), "edge_fraction": float(mask.mean()),
    }
    if inputs.get("return_image", True):
        buf = io.BytesIO()
        Image.fromarray((mask * 255).astype(np.uint8), mode="L").save(buf, format="PNG")
        out["edges_png_base64"] = base64.b64encode(buf.getvalue()).decode()
    return out


OPERATIONS = {"kornia.canny": canny}
