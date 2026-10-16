from __future__ import annotations

from typing import Iterable, Sequence

import numpy as np


def recall_at_k(results: Sequence[Sequence[int]], ground_truth: Sequence[Sequence[int]], k: int = 10) -> float:
    if not results:
        return 0.0
    scores = []
    for got, gt in zip(results, ground_truth, strict=True):
        gt_set = set(int(x) for x in list(gt)[:k])
        hit = sum(1 for x in list(got)[:k] if int(x) in gt_set)
        scores.append(hit / k)
    return float(np.mean(scores))


def latency_percentiles_ms(latencies_ns: Iterable[int]) -> dict[str, float]:
    arr = np.asarray(list(latencies_ns), dtype=np.float64)
    if arr.size == 0:
        return {"lat_p50_ms": 0.0, "lat_p95_ms": 0.0, "lat_p99_ms": 0.0}
    ms = arr / 1e6
    return {
        "lat_p50_ms": float(np.percentile(ms, 50)),
        "lat_p95_ms": float(np.percentile(ms, 95)),
        "lat_p99_ms": float(np.percentile(ms, 99)),
    }
