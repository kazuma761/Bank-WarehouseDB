"""Suggestions prompt — generates 3 example questions for the connected database.

Questions are generated once per session based on the live schema. They surface
interesting angles a user might not think to ask, covering different complexity
levels (simple aggregation, joins, filtering, ranking).
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You generate example natural-language questions for a SQL database.
Output ONLY a JSON array of exactly 3 strings. No explanation, no markdown, no code block.
Example: ["How many customers signed up last month?", "Which products have never been ordered?", "Top 5 employees by sales in Q3"]"""


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def build_prompt(schema_summary: str, glossary: dict[str, str]) -> str:
    """Build a prompt that asks the LLM to generate 3 example questions.

    The schema summary should include table names and their key columns so
    the LLM can generate questions that are actually answerable. Keep it
    compact — this is a short call, not a full schema-linking run.
    """
    glossary_section = ""
    if glossary:
        terms = ", ".join(f"{k}={v}" for k, v in list(glossary.items())[:8])
        glossary_section = f"\n\nGlossary: {terms}"

    return (
        f"Database schema:\n{schema_summary}{glossary_section}\n\n"
        "Generate 3 diverse, interesting natural-language questions a business user "
        "might ask about this data. Cover different complexity levels: one simple "
        "(count/aggregate), one involving a join or filter, one involving ranking or "
        "a business insight. Return a JSON array of exactly 3 strings."
    )


def build_schema_summary(tables: list[Any]) -> str:
    """Compact schema summary for the suggestions prompt.

    Includes every table with its most important columns (PK first,
    then up to 4 non-FK data columns). Keeps the prompt short.
    """
    lines: list[str] = []
    for t in tables:
        pk_cols = [c.name for c in t.columns if getattr(c, "is_primary_key", False)]
        data_cols = [
            c.name
            for c in t.columns
            if not getattr(c, "is_primary_key", False)
            and not getattr(c, "is_foreign_key", False)
        ][:5]
        fk_cols = [c.name for c in t.columns if getattr(c, "is_foreign_key", False)][:3]

        col_parts: list[str] = []
        if pk_cols:
            col_parts.append(f"pk:{','.join(pk_cols)}")
        if data_cols:
            col_parts.append(",".join(data_cols))
        if fk_cols:
            col_parts.append(f"fk:{','.join(fk_cols)}")

        row_hint = f" (~{t.row_count:,} rows)" if getattr(t, "row_count", None) else ""
        lines.append(f"  {t.name}{row_hint}: {' | '.join(col_parts)}")

    return "\n".join(lines)
