"""Candidate result-set consistency scoring kernels.

CPU path — score_consistency_cpu: pairwise Jaccard similarity on column-name
           sets; returns mean similarity per result set.

GPU path — score_consistency_gpu: encodes each result set as a binary vector
           over the union of all seen column names, then uses torch matmul on
           CUDA to batch-compute pairwise similarities.

Factory:
    get_result_scorer() -> callable[[list[list[dict]]], list[float]]
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from bankql.analytics.mercer.kernels import USE_GPU

# ---------------------------------------------------------------------------
# Torch — optional import
# ---------------------------------------------------------------------------

try:
    if not USE_GPU:  # bankql patch: never import torch on CPU
        raise ImportError("GPU kernels disabled")
    import torch
    _TORCH_OK = True
except ImportError:
    _TORCH_OK = False

# ---------------------------------------------------------------------------
# CPU fallback — Jaccard on column-name sets
# ---------------------------------------------------------------------------

def score_consistency_cpu(
    result_sets: list[list[dict[str, Any]]],
) -> list[float]:
    """Score each result set by Jaccard similarity with all others.

    The similarity signal captures whether multiple SQL candidates return
    result sets with the same column layout — a proxy for query correctness.

    Args:
        result_sets: One list of row-dicts per candidate.  Each row-dict
                     maps column name → value.

    Returns:
        List of mean Jaccard scores (one per result set).
        An empty list in → an empty list out.
        A single result set → [1.0] (trivially consistent with itself).
    """
    if not result_sets:
        return []

    # Extract lowercase column-name sets
    col_sets: list[frozenset[str]] = [
        frozenset(k.lower() for row in rs for k in row)
        for rs in result_sets
    ]

    n = len(col_sets)
    scores: list[float] = []

    for i in range(n):
        if n == 1:
            scores.append(1.0)
            continue
        jaccard_sum = 0.0
        for j in range(n):
            if i == j:
                continue
            a, b = col_sets[i], col_sets[j]
            union = a | b
            if not union:
                jaccard_sum += 1.0
            else:
                jaccard_sum += len(a & b) / len(union)
        scores.append(jaccard_sum / (n - 1))

    return scores


# ---------------------------------------------------------------------------
# GPU path — binary encoding + torch matmul
# ---------------------------------------------------------------------------

def score_consistency_gpu(
    result_sets: list[list[dict[str, Any]]],
) -> list[float]:
    """Batch pairwise consistency scoring using CUDA matmul.

    Encodes each result set as a binary bag-of-column-names vector over the
    union vocabulary, then computes pairwise dot products in a single
    ``torch.mm`` call.  Normalises to [0, 1] using the union-size (Jaccard
    approximation).

    Args:
        result_sets: One list of row-dicts per candidate.

    Returns:
        List of mean similarity scores, one per result set.
    """
    if not _TORCH_OK:
        raise RuntimeError(
            "torch is required for score_consistency_gpu. "
            "Install requirements-gpu.txt or use score_consistency_cpu."
        )

    if not result_sets:
        return []

    n = len(result_sets)
    if n == 1:
        return [1.0]

    # Build shared vocabulary: all lowercase column names seen across all sets
    all_cols: list[str] = sorted({
        k.lower()
        for rs in result_sets
        for row in rs
        for k in row
    })
    if not all_cols:
        return [1.0] * n

    col_index = {c: i for i, c in enumerate(all_cols)}
    V = len(all_cols)

    # Build binary indicator matrix [n, V] on CPU then move to CUDA
    import numpy as np
    mat = np.zeros((n, V), dtype=np.float32)
    for i, rs in enumerate(result_sets):
        for row in rs:
            for k in row:
                idx = col_index.get(k.lower())
                if idx is not None:
                    mat[i, idx] = 1.0

    mat_t = torch.from_numpy(mat).cuda()   # [n, V]

    # Pairwise dot products: [n, n] — counts shared columns
    dot = torch.mm(mat_t, mat_t.T)         # [n, n]
    # Union size: |A| + |B| - |A ∩ B|
    row_sums = mat_t.sum(dim=1, keepdim=True)  # [n, 1]
    union = row_sums + row_sums.T - dot    # [n, n]

    # Jaccard: dot / union, guard division by zero
    jaccard = torch.where(union > 0, dot / union, torch.zeros_like(dot))

    # Mean similarity excluding self-comparison
    diag_mask = torch.eye(n, device="cuda", dtype=torch.bool)
    jaccard.masked_fill_(diag_mask, 0.0)
    mean_sim = jaccard.sum(dim=1) / (n - 1)

    return mean_sim.cpu().tolist()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_result_scorer() -> Callable[[list[list[dict[str, Any]]]], list[float]]:
    """Return the appropriate result scorer based on GPU availability.

    Returns:
        ``score_consistency_gpu`` when USE_GPU is True,
        ``score_consistency_cpu`` otherwise.
    """
    if USE_GPU:
        return score_consistency_gpu
    return score_consistency_cpu
