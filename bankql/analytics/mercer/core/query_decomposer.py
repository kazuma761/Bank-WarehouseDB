"""Stage 3 — Query decomposition.

QueryDecomposer uses an LLM to break a natural-language question into
structured SQL planning components (aggregations, filters, joins, ordering,
subqueries) before the SQL generation stage.
"""

from __future__ import annotations

import json

from bankql.analytics.mercer.core.logging import get_logger
from bankql.analytics.mercer.core.models import FilteredSchema, QueryPlan
from bankql.analytics.mercer.core.utils import strip_json_fences
from bankql.analytics.mercer.inference.base import LLMBackend
from bankql.analytics.mercer.prompts.query_plan import SYSTEM_PROMPT, build_prompt

logger = get_logger(__name__)

_FALLBACK_PLAN = QueryPlan(
    aggregations=[],
    filters=[],
    joins=[],
    ordering=None,
    subqueries=[],
    reasoning="decomposition failed, proceeding with direct generation",
)


def _parse_plan(raw: str) -> QueryPlan | None:
    """Parse a raw LLM response into a QueryPlan. Returns None on failure."""
    try:
        data = json.loads(strip_json_fences(raw))

        def _str_list(val: object) -> list[str]:
            if isinstance(val, list):
                return [str(v) for v in val]
            return []

        ordering = data.get("ordering")
        return QueryPlan(
            aggregations=_str_list(data.get("aggregations")),
            filters=_str_list(data.get("filters")),
            joins=_str_list(data.get("joins")),
            ordering=str(ordering) if ordering else None,
            subqueries=_str_list(data.get("subqueries")),
            reasoning=str(data.get("reasoning", "")),
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


class QueryDecomposer:
    """Stage 3: decompose a question into structured SQL planning components."""

    def __init__(self, llm_backend: LLMBackend) -> None:
        self._backend = llm_backend

    async def decompose(
        self,
        question: str,
        filtered_schema: FilteredSchema,
    ) -> QueryPlan:
        """Call the LLM to produce a QueryPlan for the given question and schema.

        On JSON parse failure, returns a fallback plan with empty lists so
        downstream SQL generation can proceed unblocked.
        """
        prompt = build_prompt(question, filtered_schema)

        raw = await self._backend.generate(
            prompt=prompt,
            system=SYSTEM_PROMPT,
            temperature=0.0,
            max_tokens=512,
            is_json=True,
        )

        plan = _parse_plan(raw)

        if plan is None:
            logger.warning(
                "query_decomposition_parse_error",
                raw_response=raw[:200],
            )
            return _FALLBACK_PLAN

        logger.debug(
            "query_decomposition_complete",
            aggregations=len(plan.aggregations),
            filters=len(plan.filters),
            joins=len(plan.joins),
            subqueries=len(plan.subqueries),
        )
        return plan
