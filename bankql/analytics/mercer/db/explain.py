"""ExplainRunner — database-specific EXPLAIN / dry-run abstraction.

Used as a cheap pre-flight check before real candidate execution. EXPLAIN
parses the SQL, validates references, and produces a plan without scanning
or returning rows. When EXPLAIN succeeds, the SQL is at least structurally
sound; when it fails, we short-circuit straight to a failed result and
avoid spending a real execution slot on broken SQL.

This is one of the cheap, high-value gates the candidate-generation pipeline
needs: with 3 candidates per question, one or more is often syntactically
broken, and EXPLAIN catches them near-free.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine


class ExplainResult(BaseModel):
    valid: bool
    plan: str
    error: str | None = None


# EXPLAIN SQL templates per dialect. Unknown dialects fall back to the
# generic "EXPLAIN {sql}" form which most engines accept.
_EXPLAIN_TEMPLATES: dict[str, str] = {
    "postgresql": "EXPLAIN (FORMAT JSON) {sql}",
    "sqlite":     "EXPLAIN QUERY PLAN {sql}",
    "mysql":      "EXPLAIN FORMAT=JSON {sql}",
    "duckdb":     "EXPLAIN {sql}",
}


def _build_explain_sql(sql: str, dialect: str) -> str:
    """Render the dialect-appropriate EXPLAIN wrapper for *sql*."""
    template = _EXPLAIN_TEMPLATES.get(dialect.lower(), "EXPLAIN {sql}")
    return template.format(sql=sql)


# ---------------------------------------------------------------------------
# Public API — single entry point that handles both async and sync engines.
# ---------------------------------------------------------------------------

async def explain(
    sql: str,
    engine: AsyncEngine | Engine,
    dialect: str | None = None,
) -> ExplainResult:
    """Run EXPLAIN against *engine* and return an ExplainResult.

    Args:
        sql:     The SQL to validate (a SELECT or CTE-with-SELECT).
        engine:  Async or sync SQLAlchemy engine.
        dialect: Override the engine's dialect. When None, uses
                 ``engine.dialect.name`` directly.

    Returns:
        ExplainResult with valid=True and the plan text on success,
        or valid=False with the database error message on failure.
        Never raises — this is meant to be a cheap, soft gate.
    """
    actual_dialect = dialect or engine.dialect.name
    explain_sql = _build_explain_sql(sql, actual_dialect)

    if isinstance(engine, AsyncEngine):
        return await _explain_async(explain_sql, engine)
    return await asyncio.to_thread(_explain_sync, explain_sql, engine)


async def _explain_async(explain_sql: str, engine: AsyncEngine) -> ExplainResult:
    try:
        async with engine.connect() as conn:
            result = await conn.execute(text(explain_sql))
            rows = result.fetchall()
            plan = "\n".join(str(row) for row in rows)
        return ExplainResult(valid=True, plan=plan)
    except Exception as exc:  # noqa: BLE001
        return ExplainResult(valid=False, plan="", error=str(exc))


def _explain_sync(explain_sql: str, engine: Engine) -> ExplainResult:
    try:
        with engine.connect() as conn:
            result = conn.execute(text(explain_sql))
            rows = result.fetchall()
            plan = "\n".join(str(row) for row in rows)
        return ExplainResult(valid=True, plan=plan)
    except Exception as exc:  # noqa: BLE001
        return ExplainResult(valid=False, plan="", error=str(exc))


# ---------------------------------------------------------------------------
# Class-style API retained for backward compatibility with any existing callers.
# ---------------------------------------------------------------------------

class ExplainRunner:
    """Object-style wrapper around ``explain()``.

    Kept for callers that prefer dependency-injected runners; new code
    should call ``explain()`` directly.
    """

    async def explain(
        self,
        sql: str,
        engine: AsyncEngine | Engine,
        dialect: str | None = None,
    ) -> ExplainResult:
        return await explain(sql, engine, dialect)
