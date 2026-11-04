"""Column selection prompt — CHESS Step 3.

Given the question, entity context, and the LLM-selected tables, the model
identifies the minimum set of columns needed.

Expected LLM output (JSON only, no prose):
    {"selected_columns": {"table1": ["col1", "col2"], "table2": ["col3"]},
     "reasoning": "brief explanation"}
"""

from __future__ import annotations

from bankql.analytics.mercer.core.models import EntityContext, TableSchema
from bankql.analytics.mercer.prompts.m_schema import format_table

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a database expert. Given a natural language question and selected tables, identify the minimum set of columns needed to answer it.

Output a JSON object:
{"selected_columns": {"table1": ["col1", "col2"], "table2": ["col3"]}, "reasoning": "brief explanation"}

Output only the JSON. No other text."""

# ---------------------------------------------------------------------------
# Few-shot examples
# ---------------------------------------------------------------------------

FEW_SHOT_EXAMPLES: list[dict[str, str]] = [
    {
        "question": "What is the total revenue per customer?",
        "tables": (
            "TABLE: customer\n"
            "  Columns: customer_id (PK), first_name, last_name, email, active\n"
            "\n"
            "TABLE: payment\n"
            "  Columns: payment_id (PK), customer_id (FK), staff_id (FK), "
            "rental_id (FK), amount, payment_date"
        ),
        "output": (
            '{"selected_columns": {"customer": ["customer_id", "first_name", "last_name"], '
            '"payment": ["customer_id", "amount"]}, '
            '"reasoning": "Need customer identity columns and SUM(amount) grouped by customer_id."}'
        ),
    },
    {
        "question": "Which actors appear in more than 30 films?",
        "tables": (
            "TABLE: actor\n"
            "  Columns: actor_id (PK), first_name, last_name, last_update\n"
            "\n"
            "TABLE: film_actor\n"
            "  Columns: actor_id (FK), film_id (FK), last_update"
        ),
        "output": (
            '{"selected_columns": {"actor": ["actor_id", "first_name", "last_name"], '
            '"film_actor": ["actor_id", "film_id"]}, '
            '"reasoning": "Count film_id per actor_id; surface actor name for output."}'
        ),
    },
]

# ---------------------------------------------------------------------------
# Prompt builder — schema rendering delegated to prompts.m_schema
# ---------------------------------------------------------------------------

# Alias retained in case any external import calls it; M-Schema is the source of truth.
_format_table_columns = format_table


def build_prompt(
    question: str,
    entity_context: EntityContext,
    selected_tables: list[TableSchema],
) -> str:
    """Build the column-selection prompt.

    Args:
        question:         Original natural language question.
        entity_context:   Output of Stage 1 entity retrieval.
        selected_tables:  Tables chosen in CHESS Step 2.
    """
    lines: list[str] = []

    # --- Few-shot section ---
    lines.append("--- EXAMPLES ---")
    for ex in FEW_SHOT_EXAMPLES:
        lines.append(f"Question: {ex['question']}")
        lines.append("Selected tables:")
        lines.append(ex["tables"])
        lines.append(f"Output: {ex['output']}")
        lines.append("")

    # --- Glossary expansions (if any) ---
    if entity_context.glossary_expansions:
        lines.append("--- GLOSSARY EXPANSIONS ---")
        for term, defn in entity_context.glossary_expansions.items():
            lines.append(f"  {term} = {defn}")
        lines.append("")

    # --- Entity matches summary ---
    if entity_context.entity_matches:
        lines.append("--- ENTITY MATCHES (token → table.column) ---")
        for em in entity_context.entity_matches[:10]:
            lines.append(f"  '{em.token}' → {em.table}.{em.column} (score {em.score:.2f})")
        lines.append("")

    # --- Selected tables with full column lists ---
    lines.append("--- SELECTED TABLES ---")
    for table in selected_tables:
        lines.append(_format_table_columns(table))
        lines.append("")

    # --- Question ---
    lines.append("--- QUESTION ---")
    lines.append(question)
    lines.append("")
    lines.append(
        "Select the minimum columns needed. Output JSON: "
        '{"selected_columns": {"table": ["col1", ...]}, "reasoning": "..."}'
    )

    return "\n".join(lines)
