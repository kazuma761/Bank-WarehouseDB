"""Stage 5 — Parallel candidate execution and scoring.

CandidateExecutor runs all SQL candidates concurrently, scores each one on
three dimensions, and returns the highest-scoring candidate for the pipeline.

Each candidate is gated by an EXPLAIN pre-flight (when ``use_explain_gate``
is enabled, the default). If EXPLAIN fails, the candidate is marked as a
failed execution without a real query roundtrip — saving the expensive
slot for syntactically valid candidates. EXPLAIN is also free of side
effects and respects the same read-only invariants as a regular query.

Scoring rubric (total = 1.0):
  syntax_score      0.5  — 1.0 if execution succeeded, 0.0 otherwise
  empty_score       0.2  — 0.5 if result has at least one row, 0.0 otherwise
  consistency_score 0.3  — fraction of other successful results that share
                           the same column names (agreement signal)
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from bankql.analytics.mercer.core.logging import get_logger
from bankql.analytics.mercer.core.models import ExecutionResult, SQLCandidate
from bankql.analytics.mercer.db.explain import explain as run_explain
from bankql.analytics.mercer.db.sandbox import ReadOnlySandbox
from bankql.analytics.mercer.kernels.result_score import get_result_scorer

# Module-level scorer — CPU or GPU depending on hardware
_scorer = get_result_scorer()

logger = get_logger(__name__)

_DIRECT_COT = "direct_cot"


class CandidateExecutor:
    """Execute candidates in parallel and pick the best one.

    Args:
        sandbox: The read-only sandbox used for actual query execution.
        engine:  Async SQLAlchemy engine for the target database.
        use_explain_gate: When True (default), each candidate's SQL is
            EXPLAINed before it's executed. Candidates whose EXPLAIN fails
            are short-circuited to a failed ExecutionResult without a real
            roundtrip. Set to False for unit tests with mocked sandboxes
            where EXPLAIN behavior is not under test.
    """

    def __init__(
        self,
        sandbox: ReadOnlySandbox,
        engine: AsyncEngine,
        *,
        use_explain_gate: bool = True,
    ) -> None:
        self._sandbox = sandbox
        self._engine = engine
        self._use_explain_gate = use_explain_gate

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def execute_all(
        self, candidates: list[SQLCandidate]
    ) -> list[SQLCandidate]:
        """Run all candidates through the sandbox in parallel.

        Returns a new list of SQLCandidates with execution_result populated.
        Candidates whose SQL is empty or blank are marked failed immediately
        without hitting the sandbox. When the EXPLAIN gate is enabled,
        candidates that fail EXPLAIN are also short-circuited.
        """
        async def _run(candidate: SQLCandidate) -> SQLCandidate:
            if not candidate.sql.strip():
                result = ExecutionResult(
                    success=False,
                    sql=candidate.sql,
                    row_count=0,
                    columns=[],
                    sample_rows=[],
                    error_message="empty SQL",
                )
                return candidate.model_copy(update={"execution_result": result})

            # EXPLAIN gate — soft pre-flight check. When EXPLAIN fails, we
            # mark the candidate as failed-with-explain-error and skip the
            # real execution. The downstream corrector still gets a useful
            # error message via the standard error_message field, so the
            # taxonomy classifier can still route to the right correction
            # prompt (most explain failures classify as schema_error or
            # syntax_error).
            if self._use_explain_gate:
                explain_outcome = await run_explain(
                    candidate.sql, self._engine
                )
                if not explain_outcome.valid:
                    logger.debug(
                        "explain_gate_blocked",
                        strategy=candidate.strategy,
                        error=(explain_outcome.error or "")[:120],
                    )
                    result = ExecutionResult(
                        success=False,
                        sql=candidate.sql,
                        row_count=0,
                        columns=[],
                        sample_rows=[],
                        error_message=explain_outcome.error or "EXPLAIN failed",
                    )
                    return candidate.model_copy(
                        update={"execution_result": result}
                    )

            try:
                result = await self._sandbox.execute(candidate.sql, self._engine)
            except Exception as exc:  # noqa: BLE001
                result = ExecutionResult(
                    success=False,
                    sql=candidate.sql,
                    row_count=0,
                    columns=[],
                    sample_rows=[],
                    error_message=str(exc),
                )
            return candidate.model_copy(update={"execution_result": result})

        executed: list[SQLCandidate] = list(
            await asyncio.gather(*(_run(c) for c in candidates))
        )
        return executed

    def score_candidate(
        self,
        candidate: SQLCandidate,
        all_results: list[ExecutionResult],
    ) -> float:
        """Score a candidate on syntax, result non-emptiness, and cross-candidate consistency.

        Args:
            candidate:   The candidate to score (must have execution_result set).
            all_results: execution_results of *all* candidates in this generation
                         (used to compute the consistency signal).

        Returns:
            Float score in [0.0, 1.0].
        """
        result = candidate.execution_result
        if result is None:
            return 0.0

        # Component 1 — syntax / execution success
        syntax_score = 1.0 if result.success else 0.0

        # Component 2 — result non-emptiness
        empty_score = 0.5 if result.row_count > 0 else 0.0

        # Component 3 — column-name agreement with other successful results
        consistency_score = _consistency(result, all_results)

        final = syntax_score * 0.5 + empty_score * 0.2 + consistency_score * 0.3
        logger.debug(
            "candidate_scored",
            strategy=candidate.strategy,
            syntax=syntax_score,
            empty=empty_score,
            consistency=consistency_score,
            final=round(final, 4),
        )
        return final

    async def execute_and_score(
        self, candidates: list[SQLCandidate]
    ) -> list[SQLCandidate]:
        """Execute all candidates and populate their scores. Returns scored list."""
        executed = await self.execute_all(candidates)
        all_results = [
            c.execution_result for c in executed if c.execution_result is not None
        ]
        return [
            c.model_copy(update={"score": self.score_candidate(c, all_results)})
            for c in executed
        ]

    async def select_best(
        self, candidates: list[SQLCandidate]
    ) -> SQLCandidate | None:
        """Execute, score, and return the highest-scoring candidate.

        Tie-break rule: when scores are equal, the 'direct_cot' strategy wins.

        If every candidate fails execution (all syntax_score == 0), the
        candidate with the most informative error message is returned to give
        the downstream corrector the best starting point.

        Returns None only when the input list is empty.
        """
        if not candidates:
            return None

        scored = await self.execute_and_score(candidates)
        successful = [c for c in scored if c.execution_result and c.execution_result.success]

        if not successful:
            # All failed — return best for correction (prefer direct_cot, else first)
            logger.warning(
                "all_candidates_failed",
                strategies=[c.strategy for c in scored],
            )
            return _pick_best_failed(scored)

        # Among successful: highest score, tie-break by direct_cot preference
        winner = max(successful, key=lambda c: (c.score, c.strategy == _DIRECT_COT))
        logger.info(
            "best_candidate_selected",
            strategy=winner.strategy,
            score=round(winner.score, 4),
            total_candidates=len(scored),
            successful=len(successful),
        )
        return winner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _result_to_row_list(result: ExecutionResult) -> list[dict[str, Any]]:
    """Convert an ExecutionResult to a list of row dicts for the kernel scorer.

    Uses sample_rows when available; falls back to a pseudo-row built from
    the column list so the scorer can still see the column schema.
    """
    if result.sample_rows:
        return result.sample_rows
    if result.columns:
        return [{c: None for c in result.columns}]
    return []


def _consistency(
    result: ExecutionResult,
    all_results: list[ExecutionResult],
) -> float:
    """Mean Jaccard similarity of this result's column set vs all other successful results.

    Uses kernels.result_score scorer for consistency with the GPU execution path.
    """
    if not result.success:
        return 0.0

    successful = [r for r in all_results if r.success]
    if len(successful) <= 1:
        # Only one successful result (ours) — full consistency by default
        return 1.0

    our_idx = next((i for i, r in enumerate(successful) if r is result), None)
    if our_idx is None:
        return 1.0

    result_sets = [_result_to_row_list(r) for r in successful]
    scores = _scorer(result_sets)
    return scores[our_idx]


def _pick_best_failed(candidates: list[SQLCandidate]) -> SQLCandidate:
    """Among all-failed candidates, prefer direct_cot; otherwise return first."""
    for c in candidates:
        if c.strategy == _DIRECT_COT:
            return c
    return candidates[0]
