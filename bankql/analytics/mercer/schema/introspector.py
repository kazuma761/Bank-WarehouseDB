"""SchemaIntrospector — SQLAlchemy 2.0 async schema extraction.

Uses conn.run_sync() to run synchronous inspect() calls without blocking
the event loop. Works with any SQLAlchemy async engine (PostgreSQL, SQLite,
MySQL, DuckDB).

In addition to extracting structural metadata (tables, columns, PK/FK
relationships), the introspector samples a bounded set of distinct values
per column. These samples are critical for:

  - Stage 1 entity retrieval (LSH matches user tokens against actual cell
    values, e.g. "RETAIL" or "CORP" in a `cust_seg_cd` column).
  - Stage 2 schema-linking and SQL generation (M-Schema-style prompts that
    show example values inline give the LLM crucial disambiguation context).

Sampling is bounded by:
  - SAMPLE_VALUES_PER_COLUMN: at most this many distinct values per column.
  - MAX_SAMPLED_VALUE_LEN: skip values whose string form exceeds this length
    (avoids dumping blobs and long-form text into the index).
  - _SKIPPED_TYPE_PATTERNS: column types we never sample from (binary blobs,
    JSON/XML, etc.).

``build_bm25_corpus`` is a helper that batch-tokenizes every column's text
using the GPU/CPU kernel tokenizer (kernels.schema_encode.get_tokenizer).
It is used by BM25ColumnRetriever when building the schema search index.
"""

import hashlib
import re

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine

from bankql.analytics.mercer.core.logging import get_logger
from bankql.analytics.mercer.core.models import ColumnSchema, RawSchema, TableSchema
from bankql.analytics.mercer.kernels.schema_encode import get_tokenizer

logger = get_logger(__name__)
_tokenizer = get_tokenizer()


# ---------------------------------------------------------------------------
# Sampling configuration
# ---------------------------------------------------------------------------

# Maximum number of distinct sample values stored per column.
SAMPLE_VALUES_PER_COLUMN: int = 20

# Skip individual values longer than this many characters when sampled
# from text/varchar columns (avoids dumping notes/comments/HTML blobs).
MAX_SAMPLED_VALUE_LEN: int = 80

# Type-name patterns we never sample from. Matched case-insensitively
# against the str() form of the column type.
_SKIPPED_TYPE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"BLOB", re.IGNORECASE),
    re.compile(r"BYTEA", re.IGNORECASE),
    re.compile(r"BINARY", re.IGNORECASE),
    re.compile(r"VARBINARY", re.IGNORECASE),
    re.compile(r"\bIMAGE\b", re.IGNORECASE),
    re.compile(r"\bCLOB\b", re.IGNORECASE),
    re.compile(r"\bJSON\b", re.IGNORECASE),
    re.compile(r"\bJSONB\b", re.IGNORECASE),
    re.compile(r"\bXML\b", re.IGNORECASE),
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class SchemaIntrospector:
    """Extracts a RawSchema (structure + bounded value samples) from an engine."""

    async def introspect(self, engine: AsyncEngine) -> RawSchema:
        """Inspect all tables and columns in the database and return a RawSchema.

        Includes bounded distinct-value samples per column where the column
        type is amenable. Sampling failures are logged and ignored — a
        column will simply have ``sample_values = []`` rather than the
        whole introspection failing.
        """
        db_url_hash = _hash_url(engine)

        async with engine.connect() as conn:
            tables = await conn.run_sync(_extract_tables)
            await conn.run_sync(_sample_values_for_tables, tables)

        return RawSchema(db_url_hash=db_url_hash, tables=tables)


# ---------------------------------------------------------------------------
# Internal helpers (sync — run inside conn.run_sync)
# ---------------------------------------------------------------------------

def _extract_tables(sync_conn) -> list[TableSchema]:  # type: ignore[no-untyped-def]
    inspector = inspect(sync_conn)
    tables: list[TableSchema] = []

    for table_name in inspector.get_table_names():
        pk_cols: set[str] = set(
            inspector.get_pk_constraint(table_name).get("constrained_columns", [])
        )
        # Build a map: constrained_col -> (referred_table, referred_col)
        fk_map: dict[str, tuple[str, str]] = {}
        for fk in inspector.get_foreign_keys(table_name):
            ref_table: str = fk.get("referred_table", "") or ""
            constrained: list[str] = fk.get("constrained_columns", [])
            referred: list[str] = fk.get("referred_columns", [])
            for src_col, tgt_col in zip(constrained, referred, strict=False):
                fk_map[src_col] = (ref_table, tgt_col)

        columns: list[ColumnSchema] = []
        for col in inspector.get_columns(table_name):
            col_name: str = col["name"]
            ref = fk_map.get(col_name)
            columns.append(
                ColumnSchema(
                    name=col_name,
                    type=str(col["type"]),
                    is_primary_key=col_name in pk_cols,
                    is_foreign_key=col_name in fk_map,
                    references_table=ref[0] if ref else None,
                    references_column=ref[1] if ref else None,
                    sample_values=None,  # populated by _sample_values_for_tables
                )
            )

        tables.append(TableSchema(name=table_name, columns=columns))

    return tables


def _should_skip_type(col_type: str) -> bool:
    """Return True if the column's type indicates we should not sample values."""
    return any(p.search(col_type) for p in _SKIPPED_TYPE_PATTERNS)


def _quote_ident(dialect_name: str, ident: str) -> str:
    """Quote an identifier with the dialect-appropriate quote character."""
    if dialect_name == "mysql":
        return "`" + ident.replace("`", "``") + "`"
    # PostgreSQL, SQLite, DuckDB, and most others use double quotes.
    return '"' + ident.replace('"', '""') + '"'


def _sample_values_for_tables(  # type: ignore[no-untyped-def]
    sync_conn,
    tables: list[TableSchema],
) -> None:
    """Mutate each column's ``sample_values`` in place with distinct samples.

    Issues one ``SELECT DISTINCT col FROM tbl LIMIT N`` per (table, column)
    pair where the column type is amenable. On any per-column failure
    (permission denied, view that errors, weird type), logs and sets
    ``sample_values = []`` so the column is simply skipped by downstream
    consumers rather than triggering a hard failure.
    """
    dialect_name: str = sync_conn.engine.dialect.name

    for table in tables:
        t_quoted = _quote_ident(dialect_name, table.name)
        for col in table.columns:
            if _should_skip_type(col.type):
                col.sample_values = []
                continue

            c_quoted = _quote_ident(dialect_name, col.name)
            try:
                stmt = text(
                    f"SELECT DISTINCT {c_quoted} FROM {t_quoted} "
                    f"WHERE {c_quoted} IS NOT NULL "
                    f"LIMIT {SAMPLE_VALUES_PER_COLUMN}"
                )
                result = sync_conn.execute(stmt)
                values: list[str] = []
                for row in result:
                    raw = row[0]
                    if raw is None:
                        continue
                    s = str(raw)
                    if len(s) > MAX_SAMPLED_VALUE_LEN:
                        continue
                    values.append(s)
                col.sample_values = values
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "sample_values_failed",
                    table=table.name,
                    column=col.name,
                    error=str(exc)[:120],
                )
                col.sample_values = []


def build_bm25_corpus(
    schema: RawSchema,
) -> tuple[list[tuple[str, str]], list[list[str]]]:
    """Build a BM25-ready corpus from a schema using the kernel tokenizer.

    Returns a parallel pair of lists:
      - ``index``: (table_name, column_name) for each corpus entry
      - ``tokens``: tokenized text for each entry (col name + descriptions)

    The kernel tokenizer (``kernels.schema_encode.get_tokenizer``) is used
    for consistent tokenization with the GPU encode path.
    """
    raw_texts: list[str] = []
    index: list[tuple[str, str]] = []

    for table in schema.tables:
        for col in table.columns:
            parts: list[str] = [col.name]
            if col.description:
                parts.append(col.description)
            if table.description:
                parts.append(table.description)
            raw_texts.append(" ".join(parts))
            index.append((table.name, col.name))

    token_lists = _tokenizer(raw_texts) if raw_texts else []
    return index, token_lists


def _hash_url(engine: AsyncEngine) -> str:
    """Stable cache key from connection URL (credentials masked)."""
    url_str = engine.url.render_as_string(hide_password=True)
    return hashlib.sha256(url_str.encode()).hexdigest()
