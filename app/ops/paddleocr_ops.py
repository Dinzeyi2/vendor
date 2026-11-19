"""PaddleOCR: detect and recognise text in an image (PP-OCRv5 pipeline, CPU).

Documented API (PaddleOCR 3.x): PaddleOCR(lang=..., ...).predict(image) -> results with
rec_texts / rec_scores / rec_boxes.
Models are downloaded on first use and cached under ~/.paddlex.
"""
from __future__ import annotations

import os
from typing import Any, Dict

os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

import numpy as np
from paddleocr import PaddleOCR

from app.ops._validate import choice, image

LANGS = ["en", "ch", "fr", "german", "es", "it", "pt", "ru", "japan", "korean"]
_engines: Dict[str, PaddleOCR] = {}


def _engine(lang: str) -> PaddleOCR:
    if lang not in _engines:
        # enable_mkldnn=False: PaddlePaddle 3.x's oneDNN executor raises
        # "ConvertPirAttribute2RuntimeAttribute not support" on these models; plain CPU kernels work.
        _engines[lang] = PaddleOCR(lang=lang, use_doc_orientation_classify=False,
                                   use_doc_unwarping=False, use_textline_orientation=False,
                                   enable_mkldnn=False)
    return _engines[lang]


def ocr(inputs: Dict[str, Any]) -> Dict[str, Any]:
    img = image(inputs)
    lang = choice(inputs, "lang", "en", LANGS)
    bgr = np.asarray(img)[:, :, ::-1].copy()  # PaddleOCR expects OpenCV-style BGR arrays
    lines = []
    for page in _engine(lang).predict(bgr):
        boxes = page["rec_boxes"]
        for i, (text, score) in enumerate(zip(page["rec_texts"], page["rec_scores"])):
            lines.append({"text": text, "confidence": float(score),
                          "box": [int(v) for v in boxes[i]] if len(boxes) > i else None})
    return {"lines": lines, "text": "\n".join(line["text"] for line in lines), "lang": lang}


OPERATIONS = {"paddleocr.ocr": ocr}

# Load the default English models at worker start so the first request is not a cold start.
_engine("en")
