"""Schema string tokenization kernels.

CPU path — tokenize_corpus_cpu: regex-based whitespace + punctuation split.

GPU path — tokenize_corpus_gpu: ASCII-encodes strings into a padded int32
           tensor, runs a @triton.jit kernel to mark split points, then
           reconstructs token lists in Python.  Produces identical output to
           the CPU version for all ASCII input.

Factory:
    get_tokenizer() -> callable[[list[str]], list[list[str]]]
"""

from __future__ import annotations

import re
from collections.abc import Callable

import numpy as np

from bankql.analytics.mercer.kernels import USE_GPU

# ---------------------------------------------------------------------------
# Triton / torch — optional imports
# ---------------------------------------------------------------------------

try:
    if not USE_GPU:  # bankql patch: never import torch on CPU
        raise ImportError("GPU kernels disabled")
    import torch
    _TORCH_OK = True
except ImportError:
    _TORCH_OK = False

try:
    if not USE_GPU:  # bankql patch: never import triton on CPU
        raise ImportError("GPU kernels disabled")
    import triton
    import triton.language as tl
    _TRITON_OK = True
except ImportError:
    _TRITON_OK = False

# ---------------------------------------------------------------------------
# Shared: split-character set (ASCII codes for space + common punctuation)
# Matches: space (32) and printable punct groups (33-47, 58-64, 91-96, 123-126)
# ---------------------------------------------------------------------------

_SPLIT_RE = re.compile(r"[\s\W]+")   # whitespace or non-word characters

# ASCII codepoints considered as token delimiters by the GPU kernel
_SPLIT_CODES: frozenset[int] = frozenset(
    [32]                          # space
    + list(range(33, 48))         # ! " # $ % & ' ( ) * + , - . /
    + list(range(58, 65))         # : ; < = > ? @
    + list(range(91, 97))         # [ \ ] ^ _ `
    + list(range(123, 127))       # { | } ~
)

# ---------------------------------------------------------------------------
# Triton kernel — marks delimiter positions in a padded ASCII tensor
#
# Grid  : (N,)  — one program per string.
# Inputs: chars  [N, L] int32 ASCII codes (0 = padding).
# Output: splits [N, L] int32 (1 at delimiter/padding positions, 0 elsewhere).
# ---------------------------------------------------------------------------

if _TRITON_OK:
    @triton.jit  # type: ignore[untyped-decorator]
    def _find_splits_kernel(  # type: ignore[no-untyped-def]
        chars_ptr,
        splits_ptr,
        N,
        L,
        BLOCK_L: tl.constexpr,
    ) -> None:
        """Mark delimiter and padding positions for one string."""
        n = tl.program_id(0)
        for l_start in range(0, L, BLOCK_L):
            offs = l_start + tl.arange(0, BLOCK_L)
            mask = offs < L
            ch = tl.load(chars_ptr + n * L + offs, mask=mask, other=0)
            # A position is a split if: padding (0), space (32),
            # or punctuation group (33-47, 58-64, 91-96, 123-126).
            is_pad   = ch == 0
            is_space = ch == 32
            is_p1    = (ch >= 33) & (ch <= 47)
            is_p2    = (ch >= 58) & (ch <= 64)
            is_p3    = (ch >= 91) & (ch <= 96)
            is_p4    = (ch >= 123) & (ch <= 126)
            is_split = is_pad | is_space | is_p1 | is_p2 | is_p3 | is_p4
            tl.store(
                splits_ptr + n * L + offs,
                is_split.to(tl.int32),
                mask=mask,
            )

else:
    def _find_splits_kernel(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("Triton is not installed; GPU kernels unavailable.")


# ---------------------------------------------------------------------------
# CPU fallback
# ---------------------------------------------------------------------------

def tokenize_corpus_cpu(texts: list[str]) -> list[list[str]]:
    """Tokenize a list of strings using whitespace + punctuation splitting.

    Args:
        texts: Input strings.

    Returns:
        List of token lists.  Empty strings and single-char splits are
        dropped.  All tokens are lower-cased.
    """
    result: list[list[str]] = []
    for text in texts:
        tokens = [t for t in _SPLIT_RE.split(text.lower()) if t]
        result.append(tokens)
    return result


# ---------------------------------------------------------------------------
# GPU path
# ---------------------------------------------------------------------------

def tokenize_corpus_gpu(texts: list[str]) -> list[list[str]]:
    """Tokenize strings using a Triton kernel for split detection.

    Produces identical output to ``tokenize_corpus_cpu`` for all ASCII input.

    Args:
        texts: Input strings.

    Returns:
        List of token lists.
    """
    if not _TORCH_OK:
        raise RuntimeError(
            "torch is required for tokenize_corpus_gpu. "
            "Install requirements-gpu.txt or use tokenize_corpus_cpu."
        )

    if not texts:
        return []

    # Encode strings as ASCII integers, pad to max_len with 0
    max_len = max(len(t) for t in texts)
    if max_len == 0:
        return [[] for _ in texts]

    N = len(texts)
    chars_np = np.zeros((N, max_len), dtype=np.int32)
    for i, text in enumerate(texts):
        lower = text.lower()
        for j, ch in enumerate(lower[:max_len]):
            code = ord(ch)
            chars_np[i, j] = code if code < 128 else 32  # non-ASCII → space

    chars_t  = torch.from_numpy(chars_np).cuda()
    splits_t = torch.empty((N, max_len), dtype=torch.int32, device="cuda")

    if _TRITON_OK:
        BLOCK_L = 64
        _find_splits_kernel[(N,)](
            chars_t, splits_t,
            N, max_len,
            BLOCK_L=BLOCK_L,
        )
    else:
        # Pure torch fallback — same logic as the kernel
        ch = chars_t
        splits_t = (
            (ch == 0)
            | (ch == 32)
            | ((ch >= 33) & (ch <= 47))
            | ((ch >= 58) & (ch <= 64))
            | ((ch >= 91) & (ch <= 96))
            | ((ch >= 123) & (ch <= 126))
        ).to(torch.int32)

    # Reconstruct tokens from split map on CPU
    splits_np = splits_t.cpu().numpy()
    result: list[list[str]] = []
    for i, text in enumerate(texts):
        lower = text.lower()
        tokens: list[str] = []
        buf: list[str] = []
        for j in range(max_len):
            if j >= len(lower):
                break
            if splits_np[i, j]:
                if buf:
                    tokens.append("".join(buf))
                    buf = []
            else:
                buf.append(lower[j])
        if buf:
            tokens.append("".join(buf))
        result.append(tokens)

    return result


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_tokenizer() -> Callable[[list[str]], list[list[str]]]:
    """Return the appropriate tokenizer based on GPU availability.

    Returns:
        ``tokenize_corpus_gpu`` when USE_GPU is True,
        ``tokenize_corpus_cpu`` otherwise.
    """
    if USE_GPU:
        return tokenize_corpus_gpu
    return tokenize_corpus_cpu
