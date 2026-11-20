"""Public operation catalog: Cognex names, what each does, cost, and open-source attribution.

The public names describe what an operation does. The original library-based names keep
working as compatibility aliases (ALIASES) but are not listed in the public catalog.
Attribution (BuiltOn) is served separately by GET /v1/open-source.
Results echo the name the caller sent, so existing integrations see no change.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

CREDITS_PER_OPERATION = 1


@dataclass(frozen=True)
class BuiltOn:
    project: str
    license: str
    copyright: str
    source: str


@dataclass(frozen=True)
class Entry:
    name: str        # public Cognex name
    alias: str       # original name, still accepted
    summary: str
    built_on: BuiltOn


ENTRIES = [
    Entry("text.similarity", "rapidfuzz.compare",
          "Score how similar two strings are, 0-100 (normalized Indel similarity).",
          BuiltOn("RapidFuzz", "MIT", "Copyright (c) 2020-present Max Bachmann; Copyright (c) 2011 Adam Cohen",
                  "https://github.com/rapidfuzz/RapidFuzz")),
    Entry("vectors.nearest_neighbors", "hnswlib.knn_query",
          "Find the k nearest vectors to each query vector (HNSW approximate search; l2, inner product or cosine).",
          BuiltOn("hnswlib", "Apache-2.0", "Copyright the hnswlib authors", "https://github.com/nmslib/hnswlib")),
    Entry("tabular.train_and_predict", "xgboost.train_predict",
          "Train a gradient-boosted tree model on your rows, then predict values or classes for new rows.",
          BuiltOn("XGBoost", "Apache-2.0", "Copyright (c) 2019 by Contributors (XGBoost)", "https://github.com/dmlc/xgboost")),
    Entry("optimization.solve_convex", "scs.solve",
          "Solve a linear, quadratic or conic optimization problem: minimize 1/2 x'Px + c'x subject to Ax + s = b, s in K.",
          BuiltOn("SCS", "MIT", "Copyright (c) 2012 Brendan O'Donoghue", "https://github.com/cvxgrp/scs")),
    Entry("causal.estimate_effect", "dowhy.estimate_effect",
          "Estimate the average causal effect of a treatment on an outcome from observational data, adjusting for confounders.",
          BuiltOn("DoWhy", "MIT", "Copyright (c) PyWhy contributors", "https://github.com/py-why/dowhy")),
    Entry("image.detect_edges", "kornia.canny",
          "Detect edges in an image (Canny) and return the edge mask as a PNG plus edge statistics.",
          BuiltOn("Kornia", "Apache-2.0", "Copyright the Kornia authors", "https://github.com/kornia/kornia")),
    Entry("image.extract_text", "paddleocr.ocr",
          "Read printed text in an image (OCR) and return each line with its confidence and bounding box.",
          BuiltOn("PaddleOCR and its pretrained OCR models", "Apache-2.0", "Copyright (c) 2016 PaddlePaddle Authors. All Rights Reserved.",
                  "https://github.com/PaddlePaddle/PaddleOCR")),
    Entry("math.discover_equation", "pysr.symbolic_regression",
          "Discover a readable mathematical formula that fits your data (symbolic regression).",
          BuiltOn("PySR", "Apache-2.0", "Copyright 2020 Miles Cranmer",
                  "https://github.com/MilesCranmer/PySR")),
    Entry("scheduling.school_timetable", "timefold.school_timetabling",
          "Assign lessons to timeslots and rooms with no room, teacher or student-group double-booking.",
          BuiltOn("Timefold Solver", "Apache-2.0", "Copyright Timefold BV and the Timefold Solver contributors",
                  "https://github.com/TimefoldAI/timefold-solver")),
    Entry("physics.simulate_rigid_bodies", "rapier.simulate",
          "Simulate 3-D rigid bodies (balls, boxes, capsules) under gravity with collisions, and return their final state.",
          BuiltOn("Rapier", "Apache-2.0", "Copyright 2020 Sébastien Crozet",
                  "https://github.com/dimforge/rapier")),
]

# Output fields that name the underlying engine. Omitted when the public name is used;
# returned unchanged for the original names so existing integrations see identical output.
ENGINE_FIELDS: Dict[str, tuple] = {"physics.simulate_rigid_bodies": ("rapier_version",)}

BY_NAME: Dict[str, Entry] = {e.name: e for e in ENTRIES}
ALIASES: Dict[str, str] = {e.alias: e.name for e in ENTRIES}


def canonical(name: str) -> str:
    """The public name for `name` (itself if it is already public or unknown)."""
    return ALIASES.get(name, name)


def entry(name: str) -> Optional[Entry]:
    return BY_NAME.get(canonical(name))
