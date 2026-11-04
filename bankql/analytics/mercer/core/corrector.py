"""Stage 6 — Taxonomy-guided error correction.

TaxonomyCorrector classifies the execution error on a failed SQLCandidate,
calls the LLM with a targeted correction prompt, re-executes, and repeats up
to max_iterations times.

Error classification is rule-based (no extra LLM call):
  schema_error      — "does not exist" / "column"
  syntax_error      — "syntax error" / "parse error"
  join_error        — "join" / "foreign key"
  aggregation_error — "group by" / "aggregate"
  filter_error      — "no rows" / "empty"
  logic_error       — default (syntactically valid but wrong semantics)
"""

from __future__ import annotations

import re

from sqlalchemy.ext.asyncio import AsyncEngine

from bankql.analytics.mercer.core.logging import get_logger
from bankql.analytics.mercer.core.models import (
    CorrectionStep,
    ExecutionResult,
    FilteredSchema,
    SQLCandidate,
)
from bankql.analytics.mercer.core.utils import strip_fences
from bankql.analytics.mercer.db.sandbox import ReadOnlySandbox, SecurityViolation
from bankql.analytics.mercer.inference.base import LLMBackend
from bankql.analytics.mercer.prompts.correction import ERROR_TAXONOMY, SYSTEM_PROMPT, build_correction_prompt

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Classification rules
# ---------------------------------------------------------------------------

# Each entry: (regex pattern, error_class)
# Evaluated in order; first match wins.
_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"does not exist|no such (column|table)", re.IGNORECASE), "schema_error"),
    (re.compile(r"\bcolumn\b.*not found|unknown column", re.IGNORECASE),  "schema_error"),
    (re.compile(r"syntax error|parse error|unexpected token", re.IGNORECASE), "syntax_error"),
    (re.compile(r"\bjoin\b|foreign key", re.IGNORECASE),                  "join_error"),
    (re.compile(r"group by|aggregate|not in aggregate", re.IGNORECASE),   "aggregation_error"),
    (re.compile(r"no rows|empty result", re.IGNORECASE),                   "filter_error"),
]

_DEFAULT_CLASS = "logic_error"


# ---------------------------------------------------------------------------
# Corrector
# ---------------------------------------------------------------------------

class TaxonomyCorrector:
    """Iteratively correct a failed SQL candidate using taxonomy-guided prompts.

    Args:
        llm_backend: LLM backend for generating corrected SQL.
        sandbox:     ReadOnlySandbox for executing corrected SQL.
        engine:      Async SQLAlchemy engine for the target database.
    """

    def __init__(
        self,
        llm_backend: LLMBackend,
        sandbox: ReadOnlySandbox,
        engine: AsyncEngine,
    ) -> None:
        self._backend = llm_backend
        self._sandbox = sandbox
        self._engine = engine

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def classify_error(self, error_message: str, sql: str) -> str:  # noqa: ARG002
        """Classify an error message into one of the ERROR_TAXONOMY classes.

        The ``sql`` parameter is accepted for interface symmetry but is not
        used in the current rule-based implementation.

        Returns:
            One of the ERROR_TAXONOMY keys.
        """
        for pattern, error_class in _RULES:
            if pattern.search(error_message):
                return error_class
        return _DEFAULT_CLASS

    async def correct(
        self,
        failed_candidate: SQLCandidate,
        question: str,
        filtered_schema: FilteredSchema,
        max_iterations: int = 3,
    ) -> tuple[str | None, list[CorrectionStep]]:
        """Attempt to correct a failed SQL candidate.

        Loops up to ``max_iterations`` times:
          1. Classify the current error.
          2. Build a correction prompt.
          3. Call the LLM → corrected SQL.
          4. Execute via sandbox.
          5. Record a CorrectionStep.
          6. Return on first success.

        Args:
            failed_candidate: The candidate whose execution failed.
            question:         Original natural language question.
            filtered_schema:  Filtered schema for the prompt.
            max_iterations:   Maximum correction attempts (default: 3).

        Returns:
            A tuple of (corrected_sql | None, list[CorrectionStep]).
            corrected_sql is None if all iterations failed.
        """
        log: list[CorrectionStep] = []
        current_sql = failed_candidate.sql
        current_error = (
            failed_candidate.execution_result.error_message
            if failed_candidate.execution_result and failed_candidate.execution_result.error_message
            else "unknown error"
        )
        sample_rows = (
            failed_candidate.execution_result.sample_rows
            if failed_candidate.execution_result
            else []
        )

        for iteration in range(1, max_iterations + 1):
            error_class = self.classify_error(current_error, current_sql)
            error_hint = ERROR_TAXONOMY.get(error_class, "Unknown error type.")

            logger.debug(
                "correction_attempt",
                iteration=iteration,
                error_class=error_class,
                sql_preview=current_sql[:80],
            )

            prompt = build_correction_prompt(
                question=question,
                failed_sql=current_sql,
                error_message=current_error,
                error_class=error_class,
                error_hint=error_hint,
                filtered_schema=filtered_schema,
                sample_rows=sample_rows or None,
            )

            raw = await self._backend.generate(
                prompt=prompt,
                system=SYSTEM_PROMPT,
                temperature=0.1,
            )
            corrected_sql = strip_fences(raw)

            try:
                result = await self._sandbox.execute(corrected_sql, self._engine)
            except SecurityViolation as exc:
                # Sandbox refused to run the candidate (unparseable, DDL, or
                # multi-statement). Treat as a normal failure so the loop can
                # try another correction or exhaust gracefully.
                result = ExecutionResult(
                    success=False,
                    sql=corrected_sql,
                    row_count=0,
                    columns=[],
                    sample_rows=[],
                    error_message=f"sandbox rejected: {exc}",
                )

            step = CorrectionStep(
                iteration=iteration,
                error_class=error_class,
                original_sql=current_sql,
                corrected_sql=corrected_sql,
                success=result.success,
            )
            log.append(step)

            if result.success:
                logger.info(
                    "correction_succeeded",
                    iteration=iteration,
                    error_class=error_class,
                )
                return corrected_sql, log

            # Prepare for next iteration
            current_sql = corrected_sql
            current_error = result.error_message or "unknown error"
            sample_rows = result.sample_rows

            logger.debug(
                "correction_failed",
                iteration=iteration,
                new_error=current_error[:120],
            )

        logger.warning(
            "correction_exhausted",
            max_iterations=max_iterations,
            last_error=current_error[:120],
        )
        return None, log
