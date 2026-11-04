"""ReadOnlySandbox — the only permitted path to database execution.

All pipeline code must call ReadOnlySandbox.execute(). Direct engine.execute()
calls anywhere else in the codebase are a security violation.

Validation strategy:
  1. Parse the SQL with sqlglot. Reject anything that fails to parse.
  2. Walk the AST and reject any statement whose root is not a SELECT
     (or a CTE-with-SELECT, i.e. WITH ... SELECT).
  3. Reject any statement that contains DDL/DML expression nodes anywhere
     in the tree, even inside subqueries or CTEs.
  4. Reject multiple top-level statements (statement chains like
     "SELECT ... ; DROP TABLE ...").

This replaces the previous keyword-grep approach which had two failure
modes: (1) false positives when blocked keywords appeared as string
literals (e.g. SELECT * FROM tickets WHERE body LIKE '%please DROP%');
(2) brittleness against comment-injection tricks the regex couldn't see
through. AST validation is sound by construction.

Defense-in-depth:
  - PostgreSQL connections additionally run SET TRANSACTION READ ONLY.
  - statement_timeout caps runaway queries on PostgreSQL.
"""

import asyncio
import time
from typing import Any

import sqlglot
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlglot import exp

from bankql.analytics.mercer.core.models import ExecutionResult

# ---------------------------------------------------------------------------
# Security constants
# ---------------------------------------------------------------------------

# Statement-level node types that are categorically write/DDL operations.
# A SELECT statement is rejected if any of these appear *anywhere* in its tree.
_FORBIDDEN_STATEMENT_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.AlterColumn,
    exp.TruncateTable,
    exp.Merge,
)

MAX_ROWS: int = 100
TIMEOUT_SECONDS: int = 5
SAMPLE_ROWS: int = 3

# Dialects we explicitly try, in order. sqlglot will fall back to its
# generic parser if none of these accept the input, which is what we want.
_PARSE_DIALECTS: tuple[str | None, ...] = (None, "postgres", "mysql", "sqlite", "duckdb")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class SecurityViolation(Exception):
    """Raised when SQL fails read-only validation (DDL/DML, multi-statement, or unparseable)."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _parse_statements(sql: str) -> list[Any]:
    """Parse SQL into a list of top-level statement expressions.

    Tries multiple dialects and returns the first parse that produces
    a non-empty statement list. The element type is sqlglot.exp.Expression
    (or any subclass — Select, Union, With, etc.); annotated as plain
    ``list`` because sqlglot's parse() return type is loose enough that
    a stricter annotation would require ignoring per-call type errors
    at every callsite without adding real safety.

    Raises SecurityViolation if no dialect can parse the SQL — refusing
    to execute unparseable input is safer than passing it through.
    """
    last_err: Exception | None = None
    for dialect in _PARSE_DIALECTS:
        try:
            parsed = sqlglot.parse(sql, dialect=dialect)
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            continue
        # parsed may contain None entries for trailing semicolons or empty
        # statements — strip those before deciding.
        statements = [s for s in parsed if s is not None]
        if statements:
            return statements
    raise SecurityViolation(
        f"SQL could not be parsed by any supported dialect: {last_err}"
    )


def _validate_read_only(sql: str) -> None:
    """Validate that *sql* is a single read-only SELECT statement.

    Raises SecurityViolation on any failure. The error messages are kept
    generic on purpose so they don't leak parser internals to callers.
    """
    statements = _parse_statements(sql)

    if len(statements) > 1:
        raise SecurityViolation(
            f"Only a single statement is permitted; got {len(statements)}."
        )

    root = statements[0]

    # Top-level must be a Select, or a CTE wrapping a Select. sqlglot also
    # uses Subquery/Union as Select-equivalent containers in some grammars.
    if isinstance(root, exp.With):
        # `WITH ... SELECT` is allowed; sqlglot represents the inner
        # statement as the With expression's `this` attribute.
        inner = root.this
        if not isinstance(inner, (exp.Select, exp.Union, exp.Subquery)):
            raise SecurityViolation(
                "WITH clause must wrap a SELECT statement."
            )
    elif not isinstance(root, (exp.Select, exp.Union, exp.Subquery)):
        type_name = type(root).__name__
        raise SecurityViolation(
            f"Only SELECT statements are permitted; got {type_name}."
        )

    # Reject any forbidden node anywhere in the tree (including inside
    # subqueries, scalar correlated queries, or CTE bodies).
    for forbidden in _FORBIDDEN_STATEMENT_NODES:
        match = root.find(forbidden)
        if match is not None:
            raise SecurityViolation(
                f"SQL contains forbidden {forbidden.__name__} node; "
                "only read-only SELECT statements are permitted."
            )


# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------

class ReadOnlySandbox:
    """Executes SQL in a read-only sandboxed environment.

    Enforces:
    - AST-validated read-only SELECT (raises SecurityViolation on failure)
    - Statement timeout (PostgreSQL: SET LOCAL statement_timeout)
    - Read-only transaction (PostgreSQL: SET TRANSACTION READ ONLY)
    - Row limit: at most MAX_ROWS rows fetched
    - Returns ExecutionResult — never raw cursor objects
    - sample_rows: first SAMPLE_ROWS rows only (for LLM context)
    """

    async def execute(
        self,
        sql: str,
        engine: AsyncEngine | Engine,
    ) -> ExecutionResult:
        """Execute *sql* in the sandbox and return an ExecutionResult (never raises on SQL errors)."""
        _validate_read_only(sql)

        if isinstance(engine, AsyncEngine):
            return await self._execute_async(sql, engine)
        else:
            return await asyncio.to_thread(self._execute_sync, sql, engine)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _execute_async(self, sql: str, engine: AsyncEngine) -> ExecutionResult:
        start = time.monotonic()
        try:
            async with engine.begin() as conn:
                dialect = engine.dialect.name
                if dialect == "postgresql":
                    # Read-only transaction is the primary defense; the AST
                    # check is the secondary one. statement_timeout caps
                    # accidental long-running queries.
                    await conn.execute(text("SET TRANSACTION READ ONLY"))
                    await conn.execute(
                        text(f"SET LOCAL statement_timeout = '{TIMEOUT_SECONDS * 1000}'")
                    )
                result = await conn.execute(text(sql))
                columns = list(result.keys())
                rows = result.fetchmany(MAX_ROWS)
                row_dicts = [dict(zip(columns, row, strict=True)) for row in rows]
                elapsed = (time.monotonic() - start) * 1000
                return ExecutionResult(
                    success=True,
                    sql=sql,
                    row_count=len(row_dicts),
                    columns=columns,
                    sample_rows=row_dicts[:SAMPLE_ROWS],
                    execution_time_ms=round(elapsed, 2),
                )
        except SecurityViolation:
            raise
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            return ExecutionResult(
                success=False,
                sql=sql,
                row_count=0,
                columns=[],
                sample_rows=[],
                error_message=str(exc),
                execution_time_ms=round(elapsed, 2),
            )

    def _execute_sync(self, sql: str, engine: Engine) -> ExecutionResult:
        start = time.monotonic()
        try:
            with engine.begin() as conn:
                dialect = engine.dialect.name
                if dialect == "postgresql":
                    conn.execute(text("SET TRANSACTION READ ONLY"))
                    conn.execute(
                        text(f"SET LOCAL statement_timeout = '{TIMEOUT_SECONDS * 1000}'")
                    )
                result = conn.execute(text(sql))
                columns = list(result.keys())
                rows = result.fetchmany(MAX_ROWS)
                row_dicts = [dict(zip(columns, row, strict=True)) for row in rows]
                elapsed = (time.monotonic() - start) * 1000
                return ExecutionResult(
                    success=True,
                    sql=sql,
                    row_count=len(row_dicts),
                    columns=columns,
                    sample_rows=row_dicts[:SAMPLE_ROWS],
                    execution_time_ms=round(elapsed, 2),
                )
        except SecurityViolation:
            raise
        except Exception as exc:
            elapsed = (time.monotonic() - start) * 1000
            return ExecutionResult(
                success=False,
                sql=sql,
                row_count=0,
                columns=[],
                sample_rows=[],
                error_message=str(exc),
                execution_time_ms=round(elapsed, 2),
            )
