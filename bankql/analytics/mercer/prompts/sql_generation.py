"""SQL generation prompt — supports both full-schema (Phase 0 fallback) and
filtered-schema (Phase 1+) variants.

Phase 0 baseline:  build_prompt_full_schema(question, schema: AnnotatedSchema)
Phase 1+ default:  build_prompt(question, filtered: FilteredSchema)
"""

from __future__ import annotations

from bankql.analytics.mercer.core.models import (
    AnnotatedSchema,
    FilteredSchema,
    QueryPlan,
)
from bankql.analytics.mercer.prompts.m_schema import format_filtered_schema, format_full_schema

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are an expert SQL engineer. You generate accurate, executable SQL from natural language questions.
Rules:
- Output ONLY the SQL query. No explanation, no markdown, no backticks.
- Use only the tables and columns provided in the schema.
- Always qualify column names with table names when joining.
- Use explicit JOIN syntax, never implicit comma joins.
- Never use SELECT * — always specify columns.
"""

# ---------------------------------------------------------------------------
# Few-shot examples
# ---------------------------------------------------------------------------

FEW_SHOT_EXAMPLES: list[dict[str, str]] = [
    {
        "question": "How many customers are there?",
        "schema_summary": "customers(id INTEGER PK, name TEXT, email TEXT)",
        "sql": "SELECT COUNT(*) AS customer_count FROM customers;",
    },
    {
        "question": "List the top 3 products by revenue",
        "schema_summary": (
            "orders(id INTEGER PK, product_id INTEGER FK, amount NUMERIC), "
            "products(id INTEGER PK, name TEXT)"
        ),
        "sql": (
            "SELECT p.name, SUM(o.amount) AS revenue "
            "FROM orders o "
            "JOIN products p ON o.product_id = p.id "
            "GROUP BY p.name "
            "ORDER BY revenue DESC "
            "LIMIT 3;"
        ),
    },
]

# ---------------------------------------------------------------------------
# Schema rendering — delegates to prompts.m_schema (XiYan-SQL M-Schema format).
# Sample values from ColumnSchema.sample_values are rendered inline next to
# each column, which gives the LLM concrete examples for cryptic columns.
# ---------------------------------------------------------------------------

# Aliases preserved for any external import callers.
_format_filtered_schema = format_filtered_schema
_format_full_schema = format_full_schema


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------

def _preamble() -> list[str]:
    lines: list[str] = ["--- EXAMPLES ---"]
    for ex in FEW_SHOT_EXAMPLES:
        lines.append(f"Schema: {ex['schema_summary']}")
        lines.append(f"Question: {ex['question']}")
        lines.append(f"SQL: {ex['sql']}")
        lines.append("")
    return lines


def build_prompt(question: str, filtered: FilteredSchema) -> str:
    """Build an SQL generation prompt from a FilteredSchema (Phase 1+).

    Only the tables and columns selected by the schema linker are included.
    FK join paths are shown explicitly so the model doesn't have to infer them.
    """
    lines = _preamble()
    lines.append("--- DATABASE SCHEMA (filtered to relevant tables/columns) ---")
    lines.append(_format_filtered_schema(filtered))
    lines.append("")
    lines.append("--- QUESTION ---")
    lines.append(question)
    lines.append("")
    lines.append("SQL:")
    return "\n".join(lines)


def _format_query_plan(plan: QueryPlan) -> str:
    """Render a QueryPlan as a compact bulleted list for use in prompts."""
    lines: list[str] = []
    if plan.aggregations:
        lines.append(f"  Aggregations: {', '.join(plan.aggregations)}")
    if plan.filters:
        lines.append(f"  Filters: {', '.join(plan.filters)}")
    if plan.joins:
        lines.append(f"  Joins: {', '.join(plan.joins)}")
    if plan.ordering:
        lines.append(f"  Ordering: {plan.ordering}")
    if plan.subqueries:
        lines.append(f"  Subqueries: {', '.join(plan.subqueries)}")
    if plan.reasoning:
        lines.append(f"  Reasoning: {plan.reasoning}")
    return "\n".join(lines) if lines else "  (no plan components)"


def build_prompt_direct_cot(
    question: str,
    filtered: FilteredSchema,
    query_plan: QueryPlan,
) -> str:
    """Strategy: direct chain-of-thought.

    Instructs the LLM to reason step by step through the problem before
    writing the SQL.  The query plan is provided as a reasoning hint.
    """
    lines = _preamble()
    lines.append("--- DATABASE SCHEMA ---")
    lines.append(_format_filtered_schema(filtered))
    lines.append("")
    lines.append("--- QUERY PLAN (hints) ---")
    lines.append(_format_query_plan(query_plan))
    lines.append("")
    lines.append("--- QUESTION ---")
    lines.append(question)
    lines.append("")
    lines.append(
        "Think step by step through the tables, joins, and conditions needed, "
        "then output the final SQL query."
    )
    lines.append("SQL:")
    return "\n".join(lines)


def build_prompt_divide_conquer(
    question: str,
    filtered: FilteredSchema,
    query_plan: QueryPlan,
) -> str:
    """Strategy: divide and conquer.

    Instructs the LLM to address each SQL clause independently (SELECT,
    FROM/JOIN, WHERE, GROUP BY, ORDER BY) and then merge them into one query.
    """
    lines = _preamble()
    lines.append("--- DATABASE SCHEMA ---")
    lines.append(_format_filtered_schema(filtered))
    lines.append("")
    lines.append("--- QUERY PLAN ---")
    lines.append(_format_query_plan(query_plan))
    lines.append("")
    lines.append("--- QUESTION ---")
    lines.append(question)
    lines.append("")
    lines.append(
        "Break the query into individual clauses:\n"
        "  1. SELECT clause (columns and aggregations)\n"
        "  2. FROM / JOIN clause (tables and join conditions)\n"
        "  3. WHERE clause (filters)\n"
        "  4. GROUP BY / HAVING clause (if needed)\n"
        "  5. ORDER BY / LIMIT clause (if needed)\n"
        "Then combine all clauses into a single SQL query."
    )
    lines.append("SQL:")
    return "\n".join(lines)


def build_prompt_plan_execute(
    question: str,
    filtered: FilteredSchema,
    query_plan: QueryPlan,
) -> str:
    """Strategy: plan then execute.

    Presents the query plan as an explicit step-by-step recipe for the LLM
    to follow when constructing the SQL.
    """
    lines = _preamble()
    lines.append("--- DATABASE SCHEMA ---")
    lines.append(_format_filtered_schema(filtered))
    lines.append("")
    lines.append("--- QUESTION ---")
    lines.append(question)
    lines.append("")
    lines.append("--- EXECUTION PLAN ---")
    lines.append("Follow this query plan step by step to write the SQL:")
    lines.append(_format_query_plan(query_plan))
    lines.append("")
    lines.append(
        "Execute the plan above and output the final SQL query."
    )
    lines.append("SQL:")
    return "\n".join(lines)


def build_prompt_full_schema(question: str, schema: AnnotatedSchema) -> str:
    """Build an SQL generation prompt from a full AnnotatedSchema (Phase 0 fallback).

    Used when schema linking is unavailable (e.g. testing without Stage 1–2).
    """
    lines = _preamble()
    lines.append("--- DATABASE SCHEMA ---")
    lines.append(_format_full_schema(schema))
    lines.append("")
    lines.append("--- QUESTION ---")
    lines.append(question)
    lines.append("")
    lines.append("SQL:")
    return "\n".join(lines)
