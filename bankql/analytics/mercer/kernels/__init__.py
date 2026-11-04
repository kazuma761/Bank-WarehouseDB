"""kernels — GPU acceleration layer for Mercer.

CUDA/Triton kernels for LSH hashing, tokenization, and candidate scoring.
Every kernel has a CPU fallback so the pipeline runs without a GPU.

Exports:
    CUDA_AVAILABLE  bool — True when torch + CUDA are present and usable.
    USE_GPU         bool — Alias for CUDA_AVAILABLE; controls kernel dispatch.
"""

from __future__ import annotations

import os

from bankql.analytics.mercer.core.logging import get_logger

_logger = get_logger(__name__)

# bankql patch: torch is imported only when BANKQL_MERCER_GPU_KERNELS=1 (CPU fallbacks otherwise).
CUDA_AVAILABLE: bool = False
if os.environ.get("BANKQL_MERCER_GPU_KERNELS") == "1":
    try:
        import torch as _torch
        CUDA_AVAILABLE = _torch.cuda.is_available()
    except ImportError:
        CUDA_AVAILABLE = False

USE_GPU: bool = CUDA_AVAILABLE

if USE_GPU:
    _logger.info("GPU kernels active", cuda_device=_torch.cuda.get_device_name(0))
else:
    _logger.debug("GPU kernels off, using CPU fallbacks")

__all__ = ["CUDA_AVAILABLE", "USE_GPU"]
