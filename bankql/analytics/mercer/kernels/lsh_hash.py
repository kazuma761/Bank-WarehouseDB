"""Locality-Sensitive Hashing for entity matching.

CPU path  — CPULSHMatcher: TF-IDF vectoriser + cosine nearest-neighbours
            (scikit-learn, always available).

GPU path  — TritonLSHMatcher: character n-gram bag-of-words embeddings
            projected with H random unit vectors via a @triton.jit kernel.
            Falls back gracefully when torch/triton are not installed.

Factory:
    get_lsh_matcher(n_hashes=16) -> CPULSHMatcher | TritonLSHMatcher
"""

from __future__ import annotations

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

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
# Triton kernel — random-projection sign hashing
#
# Grid  : (N, H)  — one program per (embedding, hash-vector) pair.
# Inputs: emb  [N, D] float32 — normalised n-gram embeddings.
#         vec  [H, D] float32 — random unit projection vectors.
# Output: out  [N, H] int32  — 1 if dot >= 0, else 0.
# ---------------------------------------------------------------------------

if _TRITON_OK:
    @triton.jit  # type: ignore[untyped-decorator]
    def _lsh_projection_kernel(  # type: ignore[no-untyped-def]
        emb_ptr,
        vec_ptr,
        out_ptr,
        D,
        H,
        BLOCK_D: tl.constexpr,
    ) -> None:
        """Compute sign(emb[n] · vec[h]) for one (n, h) pair."""
        n = tl.program_id(0)
        h = tl.program_id(1)

        # Accumulate dot product in BLOCK_D-sized chunks.
        # Correctness proof: each d in [0,D) maps uniquely to one
        # (chunk, lane) pair, so tl.sum(partial) == sum_d(emb[d]*vec[d]).
        partial = tl.zeros([BLOCK_D], dtype=tl.float32)
        for d_start in range(0, D, BLOCK_D):
            offs = d_start + tl.arange(0, BLOCK_D)
            mask = offs < D
            e = tl.load(emb_ptr + n * D + offs, mask=mask, other=0.0)
            v = tl.load(vec_ptr + h * D + offs, mask=mask, other=0.0)
            partial = partial + e * v

        dot = tl.sum(partial)
        result = (dot >= 0.0).to(tl.int32)
        tl.store(out_ptr + n * H + h, result)

else:
    def _lsh_projection_kernel(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("Triton is not installed; GPU kernels unavailable.")


# ---------------------------------------------------------------------------
# CPU fallback — TF-IDF + cosine similarity (scikit-learn)
# ---------------------------------------------------------------------------

class CPULSHMatcher:
    """CPU LSH matcher using TF-IDF character n-grams + cosine similarity.

    Equivalent to MinHash LSH in terms of API but uses exact cosine NN
    for maximum accuracy on small corpora.
    """

    def __init__(self) -> None:
        self._vectorizer: TfidfVectorizer | None = None
        self._matrix: object = None          # sparse CSR matrix
        self._corpus: list[str] = []

    def fit(self, corpus: list[str]) -> None:
        """Index the corpus.

        Args:
            corpus: List of strings to build the search index over.
        """
        self._corpus = list(corpus)
        self._vectorizer = TfidfVectorizer(
            analyzer="char_wb",
            ngram_range=(2, 4),
            min_df=1,
        )
        self._matrix = self._vectorizer.fit_transform(self._corpus)

    def query(self, text: str, top_k: int = 5) -> list[tuple[str, float]]:
        """Return the top_k most similar corpus entries.

        Args:
            text:  Query string.
            top_k: Number of results to return.

        Returns:
            List of (corpus_item, similarity_score) sorted descending.
        """
        if self._vectorizer is None or self._matrix is None:
            raise RuntimeError("CPULSHMatcher.fit() must be called before query().")
        q_vec = self._vectorizer.transform([text])
        sims = cosine_similarity(q_vec, self._matrix).ravel()
        top_indices = np.argsort(sims)[::-1][:top_k]
        return [(self._corpus[i], float(sims[i])) for i in top_indices]


# ---------------------------------------------------------------------------
# GPU path — random-projection LSH with Triton kernel
# ---------------------------------------------------------------------------

# Fixed embedding dimension: char 3-gram hashed into D buckets.
_EMBED_DIM = 128
_NGRAM_N   = 3
_BLOCK_D   = 64   # Triton block size (must be power of 2, <= _EMBED_DIM)


def _ngram_embed(text: str, D: int = _EMBED_DIM, n: int = _NGRAM_N) -> np.ndarray:
    """Embed a string as a normalised bag-of-n-grams hash vector."""
    vec = np.zeros(D, dtype=np.float32)
    padded = f" {text} "
    for i in range(len(padded) - n + 1):
        gram = padded[i : i + n]
        h = hash(gram) % D
        if h < 0:
            h += D
        vec[h] += 1.0
    norm = float(np.linalg.norm(vec))
    if norm > 0.0:
        vec /= norm
    return vec


class TritonLSHMatcher:
    """Random-projection LSH using a Triton GPU kernel for the projection step.

    Architecture:
      1. fit() embeds each corpus string as a normalised D-dimensional
         bag-of-char-ngrams hash vector (CPU, NumPy).
      2. fit() projects the [N, D] embedding matrix through [H, D] random
         unit vectors via the Triton kernel → [N, H] int32 hash codes.
      3. query() embeds the query string, projects it, then ranks corpus
         entries by Hamming distance to the query hash code.

    Falls back to pure PyTorch (no Triton kernel) if triton.jit is not
    available but torch is present.
    """

    def __init__(self, n_hashes: int = 16, seed: int = 42) -> None:
        if not _TORCH_OK:
            raise RuntimeError(
                "torch is required for TritonLSHMatcher. "
                "Install requirements-gpu.txt or use CPULSHMatcher."
            )
        self._n_hashes = n_hashes
        self._D = _EMBED_DIM

        rng = np.random.default_rng(seed)
        raw = rng.standard_normal((n_hashes, self._D)).astype(np.float32)
        norms = np.linalg.norm(raw, axis=1, keepdims=True)
        self._random_vectors: np.ndarray = raw / norms  # [H, D] unit vectors

        self._corpus: list[str] = []
        self._hash_codes: torch.Tensor | None = None   # [N, H] int32 on CUDA

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def fit(self, corpus: list[str]) -> None:
        """Embed and hash the corpus on GPU.

        Args:
            corpus: List of strings to index.
        """
        self._corpus = list(corpus)
        N = len(corpus)
        if N == 0:
            self._hash_codes = None
            return

        # Build [N, D] embedding matrix on CPU
        emb_np = np.stack([_ngram_embed(s, self._D) for s in corpus])  # [N, D]
        emb_t = torch.from_numpy(emb_np).cuda()          # [N, D] float32 GPU
        vec_t = torch.from_numpy(self._random_vectors).cuda()  # [H, D] float32 GPU
        out_t = torch.empty((N, self._n_hashes), dtype=torch.int32, device="cuda")

        if _TRITON_OK:
            grid = (N, self._n_hashes)
            _lsh_projection_kernel[grid](
                emb_t, vec_t, out_t,
                self._D, self._n_hashes,
                BLOCK_D=_BLOCK_D,
            )
        else:
            # Pure torch fallback (same semantics as the kernel)
            dots = emb_t @ vec_t.T  # [N, H]
            out_t = (dots >= 0.0).to(torch.int32)

        self._hash_codes = out_t

    def query(self, text: str, top_k: int = 5) -> list[tuple[str, float]]:
        """Return top_k nearest corpus entries by Hamming distance.

        Args:
            text:  Query string.
            top_k: Number of results to return.

        Returns:
            List of (corpus_item, similarity_score) sorted descending.
            Similarity = 1 − Hamming(q, x) / n_hashes.
        """
        if self._hash_codes is None:
            raise RuntimeError("TritonLSHMatcher.fit() must be called before query().")

        q_emb = _ngram_embed(text, self._D)  # [D]
        q_t = torch.from_numpy(q_emb).cuda().unsqueeze(0)  # [1, D]
        vec_t = torch.from_numpy(self._random_vectors).cuda()

        if _TRITON_OK:
            q_out = torch.empty((1, self._n_hashes), dtype=torch.int32, device="cuda")
            _lsh_projection_kernel[(1, self._n_hashes)](
                q_t, vec_t, q_out,
                self._D, self._n_hashes,
                BLOCK_D=_BLOCK_D,
            )
        else:
            dots = q_t @ vec_t.T  # [1, H]
            q_out = (dots >= 0.0).to(torch.int32)

        # Hamming distance: count positions where query and corpus differ
        hamming = (q_out != self._hash_codes).sum(dim=1).cpu().numpy()  # [N]
        similarities = 1.0 - hamming / self._n_hashes

        top_indices = np.argsort(similarities)[::-1][:top_k]
        return [(self._corpus[i], float(similarities[i])) for i in top_indices]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_lsh_matcher(n_hashes: int = 16) -> TritonLSHMatcher | CPULSHMatcher:
    """Return the appropriate LSH matcher based on GPU availability.

    Args:
        n_hashes: Number of projection hash functions (GPU path only).

    Returns:
        TritonLSHMatcher when USE_GPU is True, CPULSHMatcher otherwise.
    """
    if USE_GPU:
        return TritonLSHMatcher(n_hashes)
    return CPULSHMatcher()
