"""Stage 4 — Async multi-candidate SQL generation.

Three strategies are dispatched concurrently via generate_batch().  Each
strategy uses a different prompt framing, temperature, and reasoning approach:

  direct_cot      — chain-of-thought reasoning, greedy (temp=0.0)
  divide_conquer  — clause-by-clause decomposition, slight sampling (temp=0.2)
  plan_execute    — follow the query plan explicitly (temp=0.3)
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TypedDict

from bankql.analytics.mercer.core.logging import get_logger
from bankql.analytics.mercer.core.models import FilteredSchema, QueryPlan, SQLCandidate
from bankql.analytics.mercer.inference.base import LLMBackend
from bankql.analytics.mercer.prompts.sql_generation import (
    SYSTEM_PROMPT,
    build_prompt_direct_cot,
    build_prompt_divide_conquer,
    build_prompt_plan_execute,
)

logger = get_logger(__name__)

_FENCE_RE = re.compile(r"^```\w*\s*\n?|\n?```\s*$", re.MULTILINE)

_PromptBuilder = Callable[[str, FilteredSchema, QueryPlan], str]


class _Strategy(TypedDict):
    name: str
    temperature: float
    builder: _PromptBuilder


STRATEGIES: list[_Strategy] = [
    {
        "name": "direct_cot",
        "temperature": 0.0,
        "builder": build_prompt_direct_cot,
    },
    {
        "name": "divide_conquer",
        "temperature": 0.2,
        "builder": build_prompt_divide_conquer,
    },
    {
        "name": "plan_execute",
        "temperature": 0.3,
        "builder": build_prompt_plan_execute,
    },
]


def _strip_fences(text: str) -> str:
    return _FENCE_RE.sub("", text).strip()


class CandidateGenerator:
    """Generate multiple SQL candidates concurrently using distinct strategies."""

    def __init__(self, llm_backend: LLMBackend) -> None:
        self._backend = llm_backend

    async def generate_candidates(
        self,
        question: str,
        filtered_schema: FilteredSchema,
        query_plan: QueryPlan,
        *,
        expanded_question: str | None = None,
    ) -> list[SQLCandidate]:
        """Build all 3 prompts, dispatch via generate_batch, return SQLCandidates.

        Args:
            question: The original natural-language question.
            filtered_schema: Tables and join paths selected by the linker.
            query_plan: Structured SQL plan from Stage 3.
            expanded_question: Optional E-SQL-style enrichment of the
                question (e.g. with inline glossary expansions). Defaults
                to ``question`` when not provided so older call sites keep
                working.

        All strategies are submitted in a single generate_batch() call so the
        backend can parallelise them.  If a strategy's response is empty or
        the batch call raises, that candidate gets sql="" and score=-1.0.
        """
        # E-SQL pattern: feed the enriched question to prompt builders. They
        # decide whether to render the enrichment alongside the original.
        prompt_question = expanded_question or question

        prompts: list[str] = []
        names: list[str] = []
        temperatures: list[float] = []

        for strat in STRATEGIES:
            prompt = strat["builder"](prompt_question, filtered_schema, query_plan)
            prompts.append(prompt)
            names.append(strat["name"])
            temperatures.append(strat["temperature"])

        # Per-strategy temperatures are honored natively now: direct_cot stays
        # greedy at 0.0, divide_conquer samples lightly at 0.2, plan_execute at
        # 0.3. This is the diversity signal the CHASE-SQL ensemble depends on.
        try:
            responses = await self._backend.generate_batch(
                prompts=prompts,
                system=SYSTEM_PROMPT,
                temperature=temperatures,
                max_tokens=1024,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "candidate_batch_failed",
                strategies=names,
                num_prompts=len(prompts),
            )
            responses = [""] * len(STRATEGIES)

        candidates: list[SQLCandidate] = []
        for name, raw in zip(names, responses, strict=True):
            sql = _strip_fences(raw)
            if sql:
                candidates.append(SQLCandidate(sql=sql, strategy=name))
            else:
                logger.warning("candidate_empty_response", strategy=name)
                candidates.append(SQLCandidate(sql="", strategy=name, score=-1.0))

        logger.debug(
            "candidates_generated",
            count=len(candidates),
            strategies=names,
            non_empty=sum(1 for c in candidates if c.sql),
        )
        return candidates
