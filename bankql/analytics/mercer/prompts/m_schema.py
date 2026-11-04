"""M-Schema — semi-structured schema representation (XiYan-SQL, Liu et al. 2024).

Compact, hierarchical schema rendering optimized for LLM consumption. Each
column is rendered as a tuple of (name:type, key/FK info, description,
inline sample values). Tables are wrapped in bracket-delimited blocks so
multi-table contexts stay parseable even with very long column lists.

Why M-Schema beats the previous TABLE/COLUMNS prose format:

  1. **Inline sample values** disambiguate cryptic columns. A column called
     ``cust_seg_cd`` is not informative on its own; ``(cust_seg_cd:TEXT,
     "RETAIL", "CORP", "SMB")`` immediately tells the model what segment
     codes look like and what enum-style filters to write.

  2. **Compact hierarchical brackets** signal nesting clearly to LLMs, which
     is robust against the schema being broken across many tokens. The
     XiYan-SQL paper showed M-Schema outperforms the flat TABLE/COLUMNS
     prose used by CHESS and DIN-SQL on the BIRD benchmark.

  3. **Single source of truth.** Previously the same _format_table was
     duplicated across sql_generation.py, query_plan.py, column_selection.py,
     and correction.py — each with subtle differences and none using
     sample_values. Centralizing here makes future iterations one-touch.

Reference: XiYan-SQL: A Multi-Generator Ensemble Framework for Text-to-SQL
(https://arxiv.org/abs/2411.08599)
"""

from __future__ import annotations

from bankql.analytics.mercer.core.models import (
    AnnotatedSchema,
    ColumnSchema,
    FilteredSchema,
    JoinPath,
    TableSchema,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Maximum number of sample values rendered inline per column. Higher gives
# more context but inflates the prompt; 5 is the XiYan-SQL paper's default
# and balances disambiguation against budget.
SAMPLES_PER_COLUMN: int = 5

# Truncate any individual sample value rendered inline to this many chars.
# Cell-value sampling already filters at MAX_SAMPLED_VALUE_LEN=80; this is
# a tighter cap for the prompt rendering specifically.
MAX_SAMPLE_RENDER_LEN: int = 40


# ---------------------------------------------------------------------------
# Internal formatters
# ---------------------------------------------------------------------------

def _render_samples(values: list[str] | None) -> str:
    """Return the inline sample-value suffix for one column.

    Returns an empty string when there are no usable samples (None or [])
    so the column tuple stays compact. When samples exist, the result is
    a comma-separated, double-quoted list capped at SAMPLES_PER_COLUMN
    items and MAX_SAMPLE_RENDER_LEN characters per item.
    """
    if not values:
        return ""
    rendered: list[str] = []
    for v in values[:SAMPLES_PER_COLUMN]:
        s = v if len(v) <= MAX_SAMPLE_RENDER_LEN else v[:MAX_SAMPLE_RENDER_LEN] + "…"
        # Escape any embedded double quotes so the rendered tuple stays
        # well-formed and unambiguous to the LLM.
        s = s.replace('"', '\\"')
        rendered.append(f'"{s}"')
    return ", ".join(rendered)


def _format_column_tuple(col: ColumnSchema) -> str:
    """Format one column as an M-Schema tuple line.

    Format: ``(name:TYPE, FLAGS, FK→tbl.col, "v1", "v2", ...) -- description``

    All sections after name:type are optional. Description (when present)
    is rendered as a trailing SQL-style comment so the model can choose
    to consume it without it interfering with the tuple itself.
    """
    parts: list[str] = [f"{col.name}:{col.type}"]

    flags: list[str] = []
    if col.is_primary_key:
        flags.append("PK")
    if col.is_foreign_key:
        flags.append("FK")
    if flags:
        parts.append("/".join(flags))

    # FK target rendered explicitly so the LLM sees the join key without
    # having to scan the JOIN PATHS section.
    if col.is_foreign_key and col.references_table and col.references_column:
        parts.append(f"→{col.references_table}.{col.references_column}")

    samples = _render_samples(col.sample_values)
    if samples:
        parts.append(samples)

    inner = ", ".join(parts)
    line = f"  ({inner})"
    if col.description:
        line += f"  -- {col.description}"
    return line


def _format_join_path(jp: JoinPath) -> str:
    """Render one JoinPath line shared across all M-Schema renderings."""
    return (
        f"  JOIN {jp.from_table} ON {jp.from_table}.{jp.from_column}"
        f" = {jp.to_table}.{jp.to_column}"
    )


# ---------------------------------------------------------------------------
# Public renderers
# ---------------------------------------------------------------------------

def format_table(table: TableSchema) -> str:
    """Render one TableSchema in M-Schema format.

    ::

        # Table: customer  -- Customer master table
        [
          (cust_id:INTEGER, PK, "1", "2", "3")
          (cust_seg_cd:TEXT, "RETAIL", "CORP", "SMB")  -- Customer segment code
          (e_add:TEXT, "alice@x.com", "bob@x.com")  -- Email — legacy field
        ]
    """
    header = f"# Table: {table.name}"
    if table.description:
        header += f"  -- {table.description}"
    body_lines = [_format_column_tuple(c) for c in table.columns]
    return header + "\n[\n" + "\n".join(body_lines) + "\n]"


def format_filtered_schema(filtered: FilteredSchema) -> str:
    """Render a FilteredSchema (Stage 2 output) in M-Schema format.

    Includes the JOIN PATHS section after the table blocks when present.
    """
    blocks = [format_table(t) for t in filtered.tables]
    text = "\n\n".join(blocks)
    if filtered.join_paths:
        jp_lines = ["", "JOIN PATHS (use these to connect the tables above):"]
        jp_lines.extend(_format_join_path(jp) for jp in filtered.join_paths)
        text += "\n" + "\n".join(jp_lines)
    return text


def format_full_schema(schema: AnnotatedSchema) -> str:
    """Render a full AnnotatedSchema (Phase 0 fallback) in M-Schema format.

    Includes the GLOSSARY section after the table blocks when present.
    """
    blocks = [format_table(t) for t in schema.tables]
    text = "\n\n".join(blocks)
    if schema.glossary:
        gloss_lines = ["", "GLOSSARY (business term → SQL meaning):"]
        gloss_lines.extend(f"  {term} = {defn}" for term, defn in schema.glossary.items())
        text += "\n" + "\n".join(gloss_lines)
    return text
