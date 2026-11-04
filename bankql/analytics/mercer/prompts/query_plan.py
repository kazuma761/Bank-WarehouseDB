"""Query decomposition prompt — CHESS Stage 3.

Given the question and the filtered schema (from Stage 2), the LLM breaks
the query into structured planning components before SQL generation.

Expected LLM output (JSON only, no prose):
{
  "aggregations": ["SUM of payment.amount grouped by customer_id"],
  "filters": ["rental_date in year 2005"],
  "joins": ["customer JOIN payment ON customer.customer_id = payment.customer_id"],
  "ordering": "total_amount DESC",
  "subqueries": [],
  "reasoning": "brief explanation"
}
"""

from __future__ import annotations

from bankql.analytics.mercer.core.models import FilteredSchema
from bankql.analytics.mercer.prompts.m_schema import format_filtered_schema

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a SQL planning expert. Break down a natural language question into structured components needed to write the SQL query.

Output a JSON object with this exact structure:
{
  "aggregations": ["SUM of payment.amount grouped by customer"],
  "filters": ["rental_date in Q1 2005", "customer.active = true"],
  "joins": ["customer JOIN rental ON customer.customer_id = rental.customer_id"],
  "ordering": "total_amount DESC",
  "subqueries": [],
  "reasoning": "This query needs a join between customer and rental, then payment for amounts"
}

If a field is not needed, use an empty list or null. Output only the JSON."""

# ---------------------------------------------------------------------------
# Few-shot examples
# ---------------------------------------------------------------------------

FEW_SHOT_EXAMPLES: list[dict[str, str]] = [
    {
        "question": "How many customers are there?",
        "schema": "TABLE: customer\n  COLUMNS: customer_id (INTEGER, PK), first_name (TEXT), last_name (TEXT), active (BOOLEAN)",
        "output": (
            '{"aggregations": ["COUNT(*) AS customer_count"], '
            '"filters": [], '
            '"joins": [], '
            '"ordering": null, '
            '"subqueries": [], '
            '"reasoning": "Simple COUNT over the customer table, no joins or filters needed."}'
        ),
    },
    {
        "question": "Show total payment amount by customer for 2005, ordered by highest first",
        "schema": (
            "TABLE: customer\n"
            "  COLUMNS: customer_id (INTEGER, PK), first_name (TEXT), last_name (TEXT)\n\n"
            "TABLE: payment\n"
            "  COLUMNS: payment_id (INTEGER, PK), customer_id (INTEGER, FK), amount (NUMERIC), payment_date (TIMESTAMP)\n\n"
            "JOIN PATHS:\n"
            "  JOIN customer ON customer.customer_id = payment.customer_id"
        ),
        "output": (
            '{"aggregations": ["SUM(payment.amount) AS total_amount grouped by customer_id, first_name, last_name"], '
            '"filters": ["EXTRACT(YEAR FROM payment.payment_date) = 2005"], '
            '"joins": ["customer JOIN payment ON customer.customer_id = payment.customer_id"], '
            '"ordering": "total_amount DESC", '
            '"subqueries": [], '
            '"reasoning": "Join customer to payment, filter by year, aggregate SUM per customer, order descending."}'
        ),
    },
    {
        "question": "Find customers who have never rented a film",
        "schema": (
            "TABLE: customer\n"
            "  COLUMNS: customer_id (INTEGER, PK), first_name (TEXT), last_name (TEXT), email (TEXT)\n\n"
            "TABLE: rental\n"
            "  COLUMNS: rental_id (INTEGER, PK), customer_id (INTEGER, FK), rental_date (TIMESTAMP)\n\n"
            "JOIN PATHS:\n"
            "  JOIN customer ON customer.customer_id = rental.customer_id"
        ),
        "output": (
            '{"aggregations": [], '
            '"filters": ["rental.rental_id IS NULL (anti-join: customers with no matching rental)"], '
            '"joins": ["customer LEFT JOIN rental ON customer.customer_id = rental.customer_id"], '
            '"ordering": "customer.customer_id ASC", '
            '"subqueries": [], '
            '"reasoning": "Anti-join pattern: LEFT JOIN rental and filter WHERE rental_id IS NULL to find customers with no rentals."}'
        ),
    },
]

# ---------------------------------------------------------------------------
# Schema formatter (compact — avoids duplicating the full column-selection output)
# ---------------------------------------------------------------------------

def _format_join_path(jp):  # type: ignore[no-untyped-def]
    """Deprecated: kept as a shim for any external callers; uses M-Schema format."""
    return (
        f"  JOIN {jp.from_table} ON {jp.from_table}.{jp.from_column}"
        f" = {jp.to_table}.{jp.to_column}"
    )


# Format delegated to prompts.m_schema (M-Schema with inline sample values).
_format_filtered_schema = format_filtered_schema


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def build_prompt(question: str, filtered_schema: FilteredSchema) -> str:
    """Build the query decomposition prompt.

    Args:
        question:        Original natural language question.
        filtered_schema: FilteredSchema from CHESS Stage 2 (only relevant tables).
    """
    lines: list[str] = []

    # Few-shot examples
    lines.append("--- EXAMPLES ---")
    for ex in FEW_SHOT_EXAMPLES:
        lines.append(f"Question: {ex['question']}")
        lines.append("Schema:")
        lines.append(ex["schema"])
        lines.append(f"Output: {ex['output']}")
        lines.append("")

    # Filtered schema
    lines.append("--- SCHEMA ---")
    lines.append(_format_filtered_schema(filtered_schema))
    lines.append("")

    # Question
    lines.append("--- QUESTION ---")
    lines.append(question)
    lines.append("")
    lines.append(
        "Break this down into SQL components. "
        'Output JSON: {"aggregations": [...], "filters": [...], '
        '"joins": [...], "ordering": "...", "subqueries": [...], "reasoning": "..."}'
    )

    return "\n".join(lines)
