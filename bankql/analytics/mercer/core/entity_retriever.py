"""Stage 1 — Entity and context retrieval.

Three components assembled by EntityRetriever:

  GlossaryExpander  — maps business terms in the question to SQL definitions
  BM25ColumnRetriever — keyword-based column search (rank_bm25)
  LSHEntityMatcher  — GPU-backed LSH similarity for value matching
                      (kernels.lsh_hash.get_lsh_matcher; falls back to CPU)
"""

from __future__ import annotations

import re
import string
from typing import Any

from rank_bm25 import BM25Okapi

from bankql.analytics.mercer.core.logging import get_logger
from bankql.analytics.mercer.core.models import AnnotatedSchema, EntityContext, EntityMatch
from bankql.analytics.mercer.kernels.lsh_hash import CPULSHMatcher, get_lsh_matcher
from bankql.analytics.mercer.kernels.schema_encode import get_tokenizer

logger = get_logger(__name__)

# Module-level tokenizer — CPU or GPU depending on hardware
_tokenizer = get_tokenizer()

# ---------------------------------------------------------------------------
# Tokenisation helpers (query-time; kernel tokenizer used for corpus build)
# ---------------------------------------------------------------------------

_PUNCT_RE = re.compile(r"[" + re.escape(string.punctuation) + r"]")


def _tokenize(text: str) -> list[str]:
    """Lowercase, strip punctuation, split on whitespace (query-side only)."""
    cleaned = _PUNCT_RE.sub(" ", text.lower())
    return [t for t in cleaned.split() if t]


# ---------------------------------------------------------------------------
# GlossaryExpander
# ---------------------------------------------------------------------------


class GlossaryExpander:
    """Expand business terms found in a question using a glossary dict.

    The glossary maps term → SQL definition, e.g.::

        {"revenue": "SUM(payment.amount)", "top customers": "ORDER BY total DESC"}

    Matching is case-insensitive substring scan over the question.
    """

    def __init__(self, glossary: dict[str, str]) -> None:
        # Sort by descending length so longer multi-word terms match first
        self._glossary = dict(
            sorted(glossary.items(), key=lambda kv: len(kv[0]), reverse=True)
        )

    def expand(self, question: str) -> dict[str, str]:
        """Return {matched_term: expansion} for all glossary hits in question."""
        q_lower = question.lower()
        return {
            term: defn
            for term, defn in self._glossary.items()
            if term.lower() in q_lower
        }

    def apply(self, question: str) -> str:
        """Return question with matching expansions appended as parentheticals."""
        hits = self.expand(question)
        if not hits:
            return question
        annotations = "; ".join(f"{t} = {d}" for t, d in hits.items())
        return f"{question} ({annotations})"


# ---------------------------------------------------------------------------
# BM25ColumnRetriever
# ---------------------------------------------------------------------------


class BM25ColumnRetriever:
    """Retrieve the most relevant (table, column) pairs for a natural-language
    question using BM25 over the schema's column names and descriptions.
    """

    def __init__(self, schema: AnnotatedSchema) -> None:
        self._schema = schema
        self._index: list[tuple[str, str]] = []   # (table_name, column_name)
        self._bm25: BM25Okapi | None = None

    def build_index(self) -> None:
        """Tokenise each column's name + description and build BM25 corpus.

        Column text is batch-processed through the kernel tokenizer
        (kernels.schema_encode.get_tokenizer) for consistent tokenization
        with the GPU encode path.
        """
        raw_texts: list[str] = []
        self._index = []

        for table in self._schema.tables:
            for col in table.columns:
                parts = [col.name]
                if col.description:
                    parts.append(col.description)
                if table.description:
                    parts.append(table.description)
                raw_texts.append(" ".join(parts))
                self._index.append((table.name, col.name))

        token_lists = _tokenizer(raw_texts)
        corpus = [toks or ["__empty__"] for toks in token_lists]
        self._bm25 = BM25Okapi(corpus)
        logger.debug("bm25_index_built", columns=len(self._index))

    def search(self, question: str, top_k: int = 20) -> list[tuple[str, str, float]]:
        """Return up to top_k (table, column, score) tuples for the question."""
        if self._bm25 is None:
            self.build_index()

        query_tokens = _tokenizer([question])[0]
        if not query_tokens:
            return []

        scores: list[float] = list(self._bm25.get_scores(query_tokens))  # type: ignore[union-attr]
        ranked = sorted(
            ((self._index[i], s) for i, s in enumerate(scores) if s > 0),
            key=lambda x: x[1],
            reverse=True,
        )
        return [(tbl, col, score) for (tbl, col), score in ranked[:top_k]]


# ---------------------------------------------------------------------------
# LSHEntityMatcher  (kernel-backed)
# ---------------------------------------------------------------------------


class LSHEntityMatcher:
    """Match question tokens against sample column values using LSH similarity.

    Uses kernels.lsh_hash.get_lsh_matcher() — CPULSHMatcher (TF-IDF + cosine)
    when no GPU is available, TritonLSHMatcher (random-projection GPU kernel)
    when CUDA is present.

    Each column's ``sample_values`` attribute (injected via SemanticMapper /
    mappings.yaml) forms the search corpus.  If absent, the column name itself
    is used as the sole sample value.
    """

    _SIMILARITY_THRESHOLD = 0.15
    _TOP_K = 5

    def __init__(self, schema: AnnotatedSchema, n_hashes: int = 8) -> None:
        self._schema = schema
        self._matcher: CPULSHMatcher = get_lsh_matcher()  # type: ignore[assignment]
        self._value_to_metas: dict[str, list[tuple[str, str]]] = {}
        self._built: bool = False

    def build_index(self) -> None:
        """Fit the LSH matcher on every sample value in the schema."""
        corpus: list[str] = []
        corpus_meta: list[tuple[str, str]] = []  # aligned with corpus

        for table in self._schema.tables:
            for col in table.columns:
                raw_samples: list[Any] = getattr(col, "sample_values", None) or [col.name]
                for v in raw_samples:
                    if v is not None:
                        corpus.append(str(v))
                        corpus_meta.append((table.name, col.name))

        self._built = True
        if not corpus:
            logger.debug("lsh_index_built", entries=0)
            return

        self._matcher.fit(corpus)
        # Build reverse lookup: value_string -> [(table, col), ...]
        for val, meta in zip(corpus, corpus_meta, strict=True):
            self._value_to_metas.setdefault(val, []).append(meta)
        logger.debug("lsh_index_built", entries=len(corpus))

    def match(self, tokens: list[str]) -> list[EntityMatch]:
        """Return EntityMatch list for tokens that hit sample values above threshold."""
        if not self._built:
            self.build_index()
        if not self._value_to_metas:
            return []

        results: list[EntityMatch] = []
        for token in tokens:
            hits = self._matcher.query(token, top_k=self._TOP_K)
            for matched_value, score in hits:
                if score < self._SIMILARITY_THRESHOLD:
                    continue
                for table, column in self._value_to_metas.get(matched_value, []):
                    results.append(
                        EntityMatch(
                            token=token,
                            table=table,
                            column=column,
                            matched_value=matched_value,
                            score=round(score, 4),
                        )
                    )

        # Deduplicate: keep highest score per (token, table, column) triple
        seen: dict[tuple[str, str, str], EntityMatch] = {}
        for em in results:
            key = (em.token, em.table, em.column)
            if key not in seen or em.score > seen[key].score:
                seen[key] = em
        return sorted(seen.values(), key=lambda x: x.score, reverse=True)


# ---------------------------------------------------------------------------
# EntityRetriever  (orchestrator)
# ---------------------------------------------------------------------------


class EntityRetriever:
    """Stage 1 orchestrator — combines glossary, BM25, and LSH into EntityContext."""

    def __init__(self, schema: AnnotatedSchema, glossary: dict[str, str]) -> None:
        self._expander = GlossaryExpander(glossary)
        self._bm25 = BM25ColumnRetriever(schema)
        self._lsh = LSHEntityMatcher(schema)
        # Eager index build so latency is paid once per session
        self._bm25.build_index()
        self._lsh.build_index()

    def retrieve(self, question: str) -> EntityContext:
        """Run all three retrieval passes and return an EntityContext."""
        # 1. Glossary expansion
        expansions = self._expander.expand(question)
        expanded_question = self._expander.apply(question)

        logger.debug(
            "entity_retrieve",
            question=question,
            glossary_hits=len(expansions),
            expanded_question=expanded_question,
        )

        # 2. BM25 column retrieval (operates on the expanded question)
        bm25_hits = self._bm25.search(expanded_question, top_k=20)
        logger.debug("bm25_hits", count=len(bm25_hits))

        # 3. LSH value matching
        tokens = _tokenize(expanded_question)
        lsh_matches = self._lsh.match(tokens)
        logger.debug("lsh_matches", count=len(lsh_matches))

        # 4. Merge: start from LSH matches, augment with BM25-derived EntityMatches
        all_matches: list[EntityMatch] = list(lsh_matches)
        lsh_keys = {(m.table, m.column) for m in lsh_matches}
        for table, column, score in bm25_hits:
            if (table, column) not in lsh_keys:
                all_matches.append(
                    EntityMatch(
                        token="",           # BM25 hits aren't tied to a single token
                        table=table,
                        column=column,
                        matched_value=column,
                        score=round(score, 4),
                    )
                )

        all_matches.sort(key=lambda x: x.score, reverse=True)

        return EntityContext(
            original_question=question,
            expanded_question=expanded_question,
            glossary_expansions=expansions,
            entity_matches=all_matches,
        )
