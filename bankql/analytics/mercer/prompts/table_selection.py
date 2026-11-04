"""Table selection prompt — CHESS Step 2.

Given a question, entity context, and candidate tables (pre-filtered by BM25
and LSH), the LLM selects the minimum set of tables needed to answer the
question.

Expected LLM output (JSON only, no prose):
    {"selected_tables": ["table1", "table2"], "reasoning": "brief explanation"}
"""

from __future__ import annotations

from bankql.analytics.mercer.core.models import EntityContext, TableSchema

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a database expert. Given a natural language question and a list of candidate tables with their descriptions and relevant columns, select ONLY the tables needed to answer the question.

Output a JSON object with this exact structure:
{"selected_tables": ["table1", "table2"], "reasoning": "brief explanation"}

Output only the JSON. No other text."""

# ---------------------------------------------------------------------------
# Few-shot examples
# ---------------------------------------------------------------------------

FEW_SHOT_EXAMPLES: list[dict[str, str]] = [
    {
        "question": "What is the total revenue per customer?",
        "candidates": (
            "TABLE: customer\n"
            "  Description: Customer master table\n"
            "  Relevant columns: customer_id (PK), first_name, last_name\n"
            "\n"
            "TABLE: payment\n"
            "  Description: Payment transactions\n"
            "  Relevant columns: payment_id (PK), customer_id (FK), amount, payment_date\n"
            "\n"
            "TABLE: rental\n"
            "  Description: Rental records\n"
            "  Relevant columns: rental_id (PK), customer_id (FK), rental_date"
        ),
        "output": '{"selected_tables": ["customer", "payment"], "reasoning": "Revenue is SUM(payment.amount), joined to customer for grouping. rental is not needed."}',
    },
    {
        "question": "Which actors appear in more than 30 films?",
        "candidates": (
            "TABLE: actor\n"
            "  Description: Actor registry\n"
            "  Relevant columns: actor_id (PK), first_name, last_name\n"
            "\n"
            "TABLE: film_actor\n"
            "  Description: Bridge table linking actors to films\n"
            "  Relevant columns: actor_id (FK), film_id (FK)\n"
            "\n"
            "TABLE: film\n"
            "  Description: Film catalogue\n"
            "  Relevant columns: film_id (PK), title"
        ),
        "output": '{"selected_tables": ["actor", "film_actor"], "reasoning": "Count films per actor using film_actor. film itself is not needed as we only count film_id occurrences."}',
    },
]

# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _format_candidate_table(
    table: TableSchema,
    relevant_col_names: set[str] | None = None,
) -> str:
    """Format one table's header + relevant columns for the prompt."""
    lines = [f"TABLE: {table.name}"]
    if table.description:
        lines.append(f"  Description: {table.description}")

    # If we have a filtered set of columns, show only those; otherwise show all
    cols_to_show = (
        [c for c in table.columns if c.name in relevant_col_names]
        if relevant_col_names
        else table.columns
    )
    if cols_to_show:
        col_parts = []
        for col in cols_to_show:
            flags = []
            if col.is_primary_key:
                flags.append("PK")
            if col.is_foreign_key:
                flags.append("FK")
            flag_str = f" ({', '.join(flags)})" if flags else ""
            desc_str = f": {col.description}" if col.description else ""
            col_parts.append(f"    {col.name}{flag_str}{desc_str}")
        lines.append("  Relevant columns:")
        lines.extend(col_parts)

    return "\n".join(lines)


def build_prompt(
    question: str,
    entity_context: EntityContext,
    all_tables: list[TableSchema],
    relevant_cols_by_table: dict[str, set[str]] | None = None,
) -> str:
    """Build the table-selection prompt.

    Args:
        question:             Original natural language question.
        entity_context:       Output of Stage 1 entity retrieval.
        all_tables:           Candidate tables (pre-filtered by BM25/LSH).
        relevant_cols_by_table: Optional map of table → set of column names to
                              highlight. When None all columns are shown.
    """
    lines: list[str] = []

    # --- Few-shot section ---
    lines.append("--- EXAMPLES ---")
    for ex in FEW_SHOT_EXAMPLES:
        lines.append(f"Question: {ex['question']}")
        lines.append("Candidate tables:")
        lines.append(ex["candidates"])
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
        for em in entity_context.entity_matches[:10]:  # cap at 10
            lines.append(f"  '{em.token}' → {em.table}.{em.column} (score {em.score:.2f})")
        lines.append("")

    # --- Candidate tables ---
    lines.append("--- CANDIDATE TABLES ---")
    for table in all_tables:
        rel_cols = (relevant_cols_by_table or {}).get(table.name)
        lines.append(_format_candidate_table(table, rel_cols))
        lines.append("")

    # --- Question ---
    lines.append("--- QUESTION ---")
    lines.append(question)
    lines.append("")
    lines.append(
        'Select only the tables needed. Output JSON: '
        '{"selected_tables": [...], "reasoning": "..."}'
    )

    return "\n".join(lines)
