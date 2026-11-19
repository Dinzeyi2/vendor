"""hnswlib: approximate nearest-neighbour search (HNSW graph).

Documented API: hnswlib.Index(space, dim) -> init_index -> add_items -> knn_query.
"""
from __future__ import annotations

from typing import Any, Dict

import hnswlib
import numpy as np

from app.errors import OperationError
from app.ops._validate import choice, integer, matrix, require


def knn_query(inputs: Dict[str, Any]) -> Dict[str, Any]:
    vectors = matrix(require(inputs, "vectors"), "vectors", max_rows=200_000, max_cols=4096)
    dim = vectors.shape[1]
    queries = matrix(require(inputs, "queries"), "queries", max_rows=10_000, cols=dim)
    n = vectors.shape[0]
    k = integer(inputs, "k", 1, 1, n)
    space = choice(inputs, "space", "l2", ["l2", "ip", "cosine"])
    ids = inputs.get("ids")
    if ids is None:
        labels = np.arange(n)
    else:
        if (not isinstance(ids, list) or len(ids) != n
                or any(isinstance(i, bool) or not isinstance(i, int) or i < 0 for i in ids)):
            raise OperationError("input 'ids' must be a list of non-negative integers, one per vector")
        if len(set(ids)) != n:
            raise OperationError("input 'ids' must be unique")
        labels = np.asarray(ids, dtype=np.int64)

    index = hnswlib.Index(space=space, dim=dim)
    index.init_index(max_elements=n, M=integer(inputs, "M", 16, 2, 100),
                     ef_construction=integer(inputs, "ef_construction", 200, 10, 2000), random_seed=100)
    index.add_items(vectors.astype(np.float32), labels, num_threads=1)
    index.set_ef(integer(inputs, "ef", max(50, k), k, 5000))
    found, distances = index.knn_query(queries.astype(np.float32), k=k, num_threads=1)
    return {"labels": found.tolist(), "distances": distances.tolist(), "space": space, "k": k}


OPERATIONS = {"hnswlib.knn_query": knn_query}
