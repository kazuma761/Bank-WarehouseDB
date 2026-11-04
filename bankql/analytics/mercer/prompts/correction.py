"""Correction prompt — Stage 6 taxonomy-guided error correction.

One prompt builder that includes the error class, hint, original SQL,
error message, filtered schema, and optional sample rows.

Expected LLM output: a single corrected SQL statement, no prose, no markdown.
"""

from __future__ import annotations

from typing import Any

from bankql.analytics.mercer.core.models import FilteredSchema
from bankql.analytics.mercer.prompts.m_schema import format_filtered_schema

# ---------------------------------------------------------------------------
# Error taxonomy
# ---------------------------------------------------------------------------

ERROR_TAXONOMY: dict[str, str] = {
    "schema_error":      "Wrong table or column name used.",
    "join_error":        "Incorrect or missing JOIN between tables.",
    "filter_error":      "Wrong WHERE clause or filter value.",
    "aggregation_error": "Incorrect GROUP BY, HAVING, or aggregate function.",
    "syntax_error":      "SQL syntax is malformed.",
    "logic_error":       "SQL is syntactically valid but semantically wrong.",
}

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are an expert SQL debugger. Fix the SQL query based on the error type provided.\n"
    "Output ONLY the corrected SQL. No explanation, no markdown."
)

# ---------------------------------------------------------------------------
# Schema formatters (compact — same style as query_plan.py)
# ---------------------------------------------------------------------------

def _format_join_path(jp):  # type: ignore[no-untyped-def]
    """Deprecated shim; M-Schema formatter is the source of truth."""
    return (
        f"  JOIN {jp.from_table} ON {jp.from_table}.{jp.from_column}"
        f" = {jp.to_table}.{jp.to_column}"
    )


# Format delegated to prompts.m_schema (M-Schema with inline sample values).
_format_filtered_schema = format_filtered_schema


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def build_correction_prompt(
    question: str,
    failed_sql: str,
    error_message: str,
    error_class: str,
    error_hint: str,
    filtered_schema: FilteredSchema,
    sample_rows: list[dict[str, Any]] | None = None,
) -> str:
    """Build a taxonomy-guided correction prompt.

    Args:
        question:        Original natural language question.
        failed_sql:      The SQL that failed execution.
        error_message:   The raw error message from the database.
        error_class:     One of the ERROR_TAXONOMY keys.
        error_hint:      Human-readable explanation of this error class.
        filtered_schema: Filtered schema (tables + join paths).
        sample_rows:     Up to 3 sample rows from a partial result, if any.
    """
    lines: list[str] = []

    # Error context
    lines.append("--- ERROR TYPE ---")
    lines.append(f"{error_class}: {error_hint}")
    lines.append("")

    lines.append("--- ORIGINAL QUESTION ---")
    lines.append(question)
    lines.append("")

    lines.append("--- FAILED SQL ---")
    lines.append(failed_sql.strip())
    lines.append("")

    lines.append("--- DATABASE ERROR ---")
    lines.append(error_message.strip())
    lines.append("")

    lines.append("--- SCHEMA ---")
    lines.append(_format_filtered_schema(filtered_schema))
    lines.append("")

    # Sample rows (at most 3; raw row data never exceeds this limit)
    if sample_rows:
        shown = sample_rows[:3]
        lines.append("--- SAMPLE ROWS (from partial result, if available) ---")
        for row in shown:
            lines.append(str(row))
        lines.append("")

    lines.append(
        "Fix the SQL to correctly answer the question. "
        "Output ONLY the corrected SQL query."
    )
    lines.append("SQL:")

    return "\n".join(lines)
