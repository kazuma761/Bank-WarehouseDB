"""CHESSSchemaLinker — 3-step schema linking (Stage 2).

Steps (mirrors the CHESS paper):
  1. Column pre-filter   — merge BM25 + LSH hits, group by table, score
  2. Table selection     — LLM selects tables from candidates
  3. Column selection    — LLM selects columns from chosen tables + adds FK paths

Reference: CHESS: Contextual Harnessing for Efficient SQL Synthesis
"""

from __future__ import annotations

import json

from bankql.analytics.mercer.core.logging import get_logger
from bankql.analytics.mercer.core.models import (
    AnnotatedSchema,
    ColumnSchema,
    EntityContext,
    FilteredSchema,
    TableSchema,
)
from bankql.analytics.mercer.core.utils import strip_json_fences
from bankql.analytics.mercer.inference.base import LLMBackend
from bankql.analytics.mercer.prompts.column_selection import SYSTEM_PROMPT as COL_SYSTEM
from bankql.analytics.mercer.prompts.column_selection import build_prompt as build_col_prompt
from bankql.analytics.mercer.prompts.table_selection import SYSTEM_PROMPT as TBL_SYSTEM
from bankql.analytics.mercer.prompts.table_selection import build_prompt as build_tbl_prompt
from bankql.analytics.mercer.schema.graph_builder import FKGraphBuilder

logger = get_logger(__name__)

# Maximum number of candidate tables forwarded to the table-selection LLM call.
_MAX_CANDIDATE_TABLES = 15
# Fallback: use this many top-scored tables when LLM returns unparseable JSON.
_FALLBACK_TOP_N = 5


class CHESSSchemaLinker:
    """Three-step schema linker that narrows a full schema to what the question needs."""

    def __init__(self, llm_backend: LLMBackend, fk_graph: FKGraphBuilder) -> None:
        self._backend = llm_backend
        self._fk_graph = fk_graph

    # ------------------------------------------------------------------
    # Public orchestrator
    # ------------------------------------------------------------------

    async def link(
        self,
        question: str,
        entity_context: EntityContext,
        schema: AnnotatedSchema,
    ) -> FilteredSchema:
        """Run the 3 CHESS steps and return a FilteredSchema."""

        # Step 1 — column pre-filter
        candidates = await self.step_column_prefilter(entity_context, schema)
        logger.debug(
            "schema_link_step1",
            candidate_tables=len(candidates),
        )

        # Step 2 — LLM table selection
        selected_tables = await self.step_table_selection(
            question, entity_context, candidates, schema
        )
        logger.debug(
            "schema_link_step2",
            selected_tables=[t.name for t in selected_tables],
        )

        # Step 3 — LLM column selection + FK join paths
        filtered = await self.step_final_columns(
            question, entity_context, selected_tables
        )
        logger.info(
            "schema_link_complete",
            tables=[t.name for t in filtered.tables],
            join_paths=len(filtered.join_paths),
        )
        return filtered

    # ------------------------------------------------------------------
    # Step 1: column pre-filter
    # ------------------------------------------------------------------

    async def step_column_prefilter(
        self,
        entity_context: EntityContext,
        schema: AnnotatedSchema,
    ) -> list[tuple[TableSchema, list[ColumnSchema], float]]:
        """Merge BM25 + LSH hits and rank tables by aggregate relevance score.

        Returns a list of (table, relevant_columns, score) triples sorted by
        score descending and capped at _MAX_CANDIDATE_TABLES.
        """
        # Accumulate scores per (table, column) from entity matches
        col_scores: dict[tuple[str, str], float] = {}
        for em in entity_context.entity_matches:
            key = (em.table, em.column)
            if em.score > col_scores.get(key, 0.0):
                col_scores[key] = em.score

        # Build a map from table name → TableSchema for fast lookup
        table_map: dict[str, TableSchema] = {t.name: t for t in schema.tables}

        # Aggregate table scores (sum of top-col scores per table)
        table_scores: dict[str, float] = {}
        relevant_cols_by_table: dict[str, set[str]] = {}
        for (tbl, col), score in col_scores.items():
            if tbl in table_map:
                table_scores[tbl] = table_scores.get(tbl, 0.0) + score
                relevant_cols_by_table.setdefault(tbl, set()).add(col)

        # If no entity matches at all, fall back to all tables with score 0
        if not table_scores:
            return [
                (t, list(t.columns), 0.0)
                for t in schema.tables[:_MAX_CANDIDATE_TABLES]
            ]

        sorted_tables = sorted(
            table_scores.items(), key=lambda kv: kv[1], reverse=True
        )

        result: list[tuple[TableSchema, list[ColumnSchema], float]] = []
        for tbl_name, score in sorted_tables[:_MAX_CANDIDATE_TABLES]:
            table = table_map[tbl_name]
            rel_col_names = relevant_cols_by_table.get(tbl_name, set())
            rel_cols = [c for c in table.columns if c.name in rel_col_names]
            result.append((table, rel_cols, round(score, 4)))

        return result

    # ------------------------------------------------------------------
    # Step 2: LLM table selection
    # ------------------------------------------------------------------

    async def step_table_selection(
        self,
        question: str,
        entity_context: EntityContext,
        candidates: list[tuple[TableSchema, list[ColumnSchema], float]],
        schema: AnnotatedSchema,
    ) -> list[TableSchema]:
        """LLM picks the tables needed from the candidate list.

        Falls back to the top _FALLBACK_TOP_N tables by score if the response
        is not valid JSON.
        """
        candidate_tables = [tbl for tbl, _, _ in candidates]
        relevant_cols_by_table: dict[str, set[str]] = {
            tbl.name: {c.name for c in cols}
            for tbl, cols, _ in candidates
        }

        prompt = build_tbl_prompt(
            question=question,
            entity_context=entity_context,
            all_tables=candidate_tables,
            relevant_cols_by_table=relevant_cols_by_table,
        )

        raw = await self._backend.generate(
            prompt=prompt,
            system=TBL_SYSTEM,
            temperature=0.1,
            max_tokens=512,
            is_json=True,
        )

        table_map: dict[str, TableSchema] = {t.name: t for t in schema.tables}
        selected_names = _parse_selected_tables(raw)

        if selected_names is None:
            # Fallback: top N by pre-filter score
            logger.warning(
                "table_selection_parse_error",
                raw_response=raw[:200],
                fallback_n=_FALLBACK_TOP_N,
            )
            return [tbl for tbl, _, _ in candidates[:_FALLBACK_TOP_N]]

        # Return full TableSchema objects (with all columns) for selected tables
        result = [
            table_map[name]
            for name in selected_names
            if name in table_map
        ]
        if not result:
            # LLM named tables that aren't in the schema — use fallback
            logger.warning(
                "table_selection_unknown_tables",
                selected=selected_names,
                fallback_n=_FALLBACK_TOP_N,
            )
            return [tbl for tbl, _, _ in candidates[:_FALLBACK_TOP_N]]

        return result

    # ------------------------------------------------------------------
    # Step 3: column selection + FK paths
    # ------------------------------------------------------------------

    async def step_final_columns(
        self,
        question: str,
        entity_context: EntityContext,
        selected_tables: list[TableSchema],
    ) -> FilteredSchema:
        """LLM picks the columns from selected tables; FK join paths are added.

        Falls back to keeping all columns when the response is not valid JSON.
        """
        prompt = build_col_prompt(
            question=question,
            entity_context=entity_context,
            selected_tables=selected_tables,
        )

        raw = await self._backend.generate(
            prompt=prompt,
            system=COL_SYSTEM,
            temperature=0.1,
            max_tokens=512,
            is_json=True,
        )

        col_map = _parse_selected_columns(raw)

        if col_map is None:
            logger.warning(
                "column_selection_parse_error",
                raw_response=raw[:200],
            )
            # Fallback: keep all columns on each selected table
            pruned_tables = selected_tables
        else:
            pruned_tables = _prune_columns(selected_tables, col_map)

        # Add FK join paths for the selected table set
        selected_names = [t.name for t in pruned_tables]
        join_paths = self._fk_graph.find_all_paths(selected_names)

        return FilteredSchema(tables=pruned_tables, join_paths=join_paths)


# ---------------------------------------------------------------------------
# JSON parsing helpers
# ---------------------------------------------------------------------------

def _parse_selected_tables(raw: str) -> list[str] | None:
    """Parse LLM response for table selection. Returns None on failure."""
    try:
        data = json.loads(strip_json_fences(raw))
        names = data.get("selected_tables")
        if isinstance(names, list) and all(isinstance(n, str) for n in names):
            return [n.strip() for n in names if n.strip()]
    except (json.JSONDecodeError, AttributeError):
        pass
    return None


def _parse_selected_columns(raw: str) -> dict[str, list[str]] | None:
    """Parse LLM response for column selection. Returns None on failure."""
    try:
        data = json.loads(strip_json_fences(raw))
        col_map = data.get("selected_columns")
        if isinstance(col_map, dict) and all(
            isinstance(k, str) and isinstance(v, list)
            for k, v in col_map.items()
        ):
            return {k: [str(c) for c in v] for k, v in col_map.items()}
    except (json.JSONDecodeError, AttributeError):
        pass
    return None


def _prune_columns(
    tables: list[TableSchema],
    col_map: dict[str, list[str]],
) -> list[TableSchema]:
    """Return new TableSchema objects with only the LLM-selected columns.

    PK and FK columns are always preserved regardless of the LLM's selection
    to keep join integrity.  Tables not mentioned in col_map are kept whole.
    """
    result: list[TableSchema] = []
    for table in tables:
        if table.name not in col_map:
            result.append(table)
            continue

        wanted = set(col_map[table.name])
        pruned = [
            col for col in table.columns
            if col.name in wanted or col.is_primary_key or col.is_foreign_key
        ]
        if not pruned:
            pruned = list(table.columns)  # safety: never return a table with zero columns
        result.append(table.model_copy(update={"columns": pruned}))
    return result
