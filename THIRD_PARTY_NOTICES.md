# Third-party notices

The Cognex API runs the following open-source software. Each project is used under its license,
reproduced in full in [`licenses/`](licenses/) (copied unmodified from each project's source).
None of these projects ships a NOTICE file in its repository root.

| Cognex operation | Original name (alias) | Project | License | Copyright | License text |
|---|---|---|---|---|---|
| `text.similarity` | `rapidfuzz.compare` | [RapidFuzz](https://github.com/rapidfuzz/RapidFuzz) | MIT | Copyright (c) 2020-present Max Bachmann; Copyright (c) 2011 Adam Cohen | [licenses/RapidFuzz-MIT.txt](licenses/RapidFuzz-MIT.txt) |
| `vectors.nearest_neighbors` | `hnswlib.knn_query` | [hnswlib](https://github.com/nmslib/hnswlib) | Apache-2.0 | Copyright the hnswlib authors | [licenses/hnswlib-Apache-2.0.txt](licenses/hnswlib-Apache-2.0.txt) |
| `tabular.train_and_predict` | `xgboost.train_predict` | [XGBoost](https://github.com/dmlc/xgboost) | Apache-2.0 | Copyright (c) 2019 by Contributors (XGBoost) | [licenses/XGBoost-Apache-2.0.txt](licenses/XGBoost-Apache-2.0.txt) |
| `optimization.solve_convex` | `scs.solve` | [SCS](https://github.com/cvxgrp/scs) | MIT | Copyright (c) 2012 Brendan O'Donoghue | [licenses/SCS-MIT.txt](licenses/SCS-MIT.txt) |
| `causal.estimate_effect` | `dowhy.estimate_effect` | [DoWhy](https://github.com/py-why/dowhy) | MIT | Copyright (c) PyWhy contributors | [licenses/DoWhy-MIT.txt](licenses/DoWhy-MIT.txt) |
| `image.detect_edges` | `kornia.canny` | [Kornia](https://github.com/kornia/kornia) | Apache-2.0 | Copyright the Kornia authors | [licenses/Kornia-Apache-2.0.txt](licenses/Kornia-Apache-2.0.txt) |
| `image.extract_text` | `paddleocr.ocr` | [PaddleOCR and its pretrained OCR models](https://github.com/PaddlePaddle/PaddleOCR) | Apache-2.0 | Copyright (c) 2016 PaddlePaddle Authors. All Rights Reserved. | [licenses/PaddleOCR-Apache-2.0.txt](licenses/PaddleOCR-Apache-2.0.txt) |
| `math.discover_equation` | `pysr.symbolic_regression` | [PySR](https://github.com/MilesCranmer/PySR) | Apache-2.0 | Copyright 2020 Miles Cranmer | [licenses/PySR-Apache-2.0.txt](licenses/PySR-Apache-2.0.txt) |
| `scheduling.school_timetable` | `timefold.school_timetabling` | [Timefold Solver](https://github.com/TimefoldAI/timefold-solver) | Apache-2.0 | Copyright Timefold BV and the Timefold Solver contributors | [licenses/TimefoldSolver-Apache-2.0.txt](licenses/TimefoldSolver-Apache-2.0.txt) |
| `physics.simulate_rigid_bodies` | `rapier.simulate` | [Rapier](https://github.com/dimforge/rapier) | Apache-2.0 | Copyright 2020 Sébastien Crozet | [licenses/Rapier-Apache-2.0.txt](licenses/Rapier-Apache-2.0.txt) |

Public attribution is kept separate from the operation catalog: `GET /v1/open-source` and the
page [docs/open-source-acknowledgements.md](docs/open-source-acknowledgements.md) (publish it as its
own page next to the API guide). Regenerate both with `python scripts/build_open_source_page.py`
and keep them, and `licenses/`, when operations are renamed or added.
