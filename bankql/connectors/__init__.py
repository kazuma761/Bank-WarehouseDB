"""Read-only database sources behind SQLAlchemy. `Source.run` / `Source.stream` validate first; there is
no other execution path. SQLite, DuckDB and PostgreSQL are implemented and tested; other SQLAlchemy dialects
(MySQL, SQL Server, Oracle, Snowflake, BigQuery, Trino) work with their driver + config — see README."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import cached_property

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine, make_url

from bankql.guardrails import Catalog, SqlasGuard, SqlValidator, TableMeta, Validation
from bankql.semantic import SemanticModel

# sqlglot dialect name for each SQLAlchemy backend name
SQLGLOT_DIALECT = {"sqlite": "sqlite", "duckdb": "duckdb", "postgresql": "postgres", "mysql": "mysql",
                   "mariadb": "mysql", "mssql": "tsql", "oracle": "oracle", "snowflake": "snowflake",
                   "bigquery": "bigquery", "trino": "trino"}


class LargeTable(BaseModel):
    time_column: str | None = None  # defaults to the semantic model's time_column


class SourceConfig(BaseModel):
    url_env: str  # the SQLAlchemy URL is read from this environment variable — never hard-coded
    dialect: str
    read_only: bool = True
    tables: list[str]  # allowlist
    semantic: list[str]
    row_cap: int = 100_000
    preview_rows: int = 100
    statement_timeout_s: float = 10.0
    large_tables: dict[str, LargeTable] = Field(default_factory=dict)
    large_scan_threshold_rows: int = 500_000
    as_of: str = "today"  # "data" (latest value of the time column), "today", or an ISO date
    as_of_table: str | None = None  # table whose time column defines "data" as_of
    description: str = ""

    @field_validator("read_only")
    @classmethod
    def _must_be_read_only(cls, v: bool) -> bool:
        if not v:
            raise ValueError("BankQL only supports read_only: true sources")
        return v


@dataclass
class QueryResult:
    sql: str
    columns: list[str]
    rows: list[list]
    truncated: bool
    elapsed_ms: float
    warnings: list[str] = field(default_factory=list)


class QueryRejected(Exception):
    def __init__(self, validation: Validation):
        super().__init__(validation.reason)
        self.validation = validation


class QueryTimeout(Exception):
    pass


def jsonable(v):
    if isinstance(v, (date, datetime)):
        return v.isoformat()
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    try:
        return float(v)  # Decimal
    except (TypeError, ValueError):
        return str(v)


class Source:
    def __init__(self, name: str, cfg: SourceConfig, semantic: SemanticModel, guard: SqlasGuard | None = None):
        self.name, self.cfg, self.semantic, self.guard = name, cfg, semantic, guard
        self.dialect = SQLGLOT_DIALECT.get(cfg.dialect, cfg.dialect)
        self._row_estimates: dict[str, int] = {}
        self._lock = threading.Lock()

    # ----------------------------------------------------------------------------------- connection

    @property
    def url(self) -> str:
        url = os.environ.get(self.cfg.url_env)
        if not url:
            raise RuntimeError(f"source {self.name!r}: environment variable {self.cfg.url_env} is not set")
        return url

    @cached_property
    def engine(self) -> Engine:
        url = make_url(self.url)
        backend = url.get_backend_name()
        if backend == "sqlite":
            path = url.database
            if not path or path == ":memory:" or not os.path.exists(path):
                raise RuntimeError(f"source {self.name!r}: SQLite file not found: {path}")

            def creator():
                con = sqlite3.connect(f"file:{os.path.abspath(path)}?mode=ro", uri=True, check_same_thread=False)
                con.execute("PRAGMA query_only = ON")
                return con
            return create_engine("sqlite://", creator=creator, pool_pre_ping=False)
        if backend == "duckdb":
            return create_engine(url, connect_args={"read_only": True})
        if backend == "postgresql":
            opts = f"-c default_transaction_read_only=on -c statement_timeout={int(self.cfg.statement_timeout_s * 1000)}"
            return create_engine(url, connect_args={"options": opts}, pool_pre_ping=True)
        return create_engine(url)  # other dialects: rely on a read-only DB role (documented)

    # ----------------------------------------------------------------------------------- catalog

    def _reflect(self) -> dict[str, dict[str, str]]:
        """table -> {column: type}. DuckDB via information_schema: duckdb_engine's SQLAlchemy 2.1 reflection
        queries pg_catalog.pg_collation, which DuckDB 2.0 lacks."""
        with self.engine.connect() as con:
            if self.dialect == "duckdb":
                rows = con.execute(text("SELECT table_name, column_name, data_type FROM information_schema.columns "
                                        "WHERE table_schema = current_schema() ORDER BY table_name, ordinal_position")).fetchall()
                out: dict[str, dict[str, str]] = {}
                for t, c, typ in rows:
                    out.setdefault(t, {})[c] = typ
                return out
        insp = inspect(self.engine)
        return {t: {c["name"]: str(c["type"]) for c in insp.get_columns(t)} for t in insp.get_table_names()}

    @cached_property
    def catalog(self) -> Catalog:
        reflected = self._reflect()
        present = {t.lower(): t for t in reflected}
        tables = {}
        for t in self.cfg.tables:
            real = present.get(t.lower())
            sem = self.semantic.table(t)
            if real is None:
                if sem is not None and sem.optional:
                    continue
                raise RuntimeError(f"source {self.name!r}: allowlisted table {t!r} not found")
            cols = reflected[real]
            lt = {k.lower(): v for k, v in self.cfg.large_tables.items()}.get(t.lower())
            tables[(None, real.lower())] = TableMeta(
                name=real, columns=cols,
                pii={c.lower() for c, col in (sem.columns.items() if sem else []) if col.pii},
                time_column=(lt.time_column if lt and lt.time_column else (sem.time_column if sem else None)),
                large=lt is not None)
        return Catalog(tables)

    @cached_property
    def validator(self) -> SqlValidator:
        return SqlValidator(self.dialect, self.catalog, scan_threshold_rows=self.cfg.large_scan_threshold_rows,
                            estimate_rows=self.estimate_rows, as_of_hint=lambda: self.as_of().isoformat(), sqlas=self.guard)

    def has_table(self, name: str) -> bool:
        return (None, name.lower()) in self.catalog.tables

    def describe_table(self, name: str) -> dict:
        meta = self.catalog.tables.get((None, name.lower()))
        if meta is None:
            raise KeyError(name)
        sem = self.semantic.table(meta.name)
        return {"table": meta.name, "description": sem.description if sem else "", "time_column": meta.time_column,
                "large": meta.large, "columns": [
                    {"name": c, "type": t, "description": (sem.columns.get(c).description if sem and c in sem.columns else ""),
                     "valid_values": (sem.columns.get(c).valid_values if sem and c in sem.columns else {}),
                     "pii": c.lower() in meta.pii} for c, t in meta.columns.items()]}

    def estimate_rows(self, meta: TableMeta) -> int:
        """Row estimate for a full scan of *meta*: EXPLAIN on Postgres/DuckDB; MAX(rowid) on SQLite (its
        EXPLAIN QUERY PLAN has no estimates). Cached."""
        key = meta.name.lower()
        if key not in self._row_estimates:
            q = f'"{meta.name}"'
            with self.engine.connect() as con:
                if self.dialect == "postgres":
                    plan = con.execute(text(f"EXPLAIN (FORMAT JSON) SELECT * FROM {q}")).scalar()
                    plan = plan if isinstance(plan, list) else json.loads(plan)
                    est = int(plan[0]["Plan"]["Plan Rows"])
                elif self.dialect == "duckdb":
                    raw = con.execute(text(f"EXPLAIN (FORMAT JSON) SELECT * FROM {q}")).fetchall()[0][1]
                    est = int(re.search(r'"Estimated Cardinality":\s*"?(\d+)', raw).group(1))
                elif self.dialect == "sqlite":
                    est = int(con.execute(text(f"SELECT COALESCE(MAX(rowid), 0) FROM {q}")).scalar())
                else:  # shortcut: generic COUNT(*); add the dialect's EXPLAIN parsing when a large source needs it
                    est = int(con.execute(text(f"SELECT COUNT(*) FROM {q}")).scalar())
            self._row_estimates[key] = est
        return self._row_estimates[key]

    def as_of(self) -> date:
        v = self.cfg.as_of
        if v == "today":
            return date.today()
        if v != "data":
            return date.fromisoformat(v)
        if not hasattr(self, "_as_of"):
            table = self.cfg.as_of_table or next(iter(self.cfg.large_tables), None)
            meta = self.catalog.tables[(None, table.lower())]
            with self.engine.connect() as con:
                d = con.execute(text(f'SELECT MAX("{meta.time_column}") FROM "{meta.name}"')).scalar()
            self._as_of = d if isinstance(d, date) else date.fromisoformat(str(d)[:10])
        return self._as_of

    # ----------------------------------------------------------------------------------- execution

    def validate(self, sql: str, *, allow_pii_output: bool = False) -> Validation:
        return self.validator.validate(sql, allow_pii_output=allow_pii_output)

    def _checked(self, sql: str, allow_pii_output: bool) -> Validation:
        v = self.validate(sql, allow_pii_output=allow_pii_output)
        if not v.ok:
            raise QueryRejected(v)
        return v

    def run(self, sql: str, params: dict | None = None, *, max_rows: int | None = None,
            allow_pii_output: bool = False) -> QueryResult:
        v = self._checked(sql, allow_pii_output)
        cap = min(max_rows or self.cfg.row_cap, self.cfg.row_cap)
        t0 = time.perf_counter()
        with self._execution(v.sql, params or {}) as result:
            cols = list(result.keys())
            rows = [list(r) for r in result.fetchmany(cap + 1)]
        truncated = len(rows) > cap
        return QueryResult(v.sql, cols, rows[:cap], truncated, round((time.perf_counter() - t0) * 1000, 1), v.warnings)

    def stream(self, sql: str, params: dict | None = None, *, batch: int = 10_000,
               allow_pii_output: bool = False) -> Iterator[tuple[list[str], list[list]]]:
        """Yield (columns, rows) batches of the full result (up to row_cap) for exports."""
        v = self._checked(sql, allow_pii_output)
        sent = 0
        with self._execution(v.sql, params or {}) as result:
            cols = list(result.keys())
            while sent < self.cfg.row_cap:
                rows = result.fetchmany(min(batch, self.cfg.row_cap - sent))
                if not rows:
                    break
                sent += len(rows)
                yield cols, [list(r) for r in rows]

    def _execution(self, sql: str, params: dict):
        return _Execution(self, sql, params)


class _Execution:
    """Context manager: one read-only transaction with a statement timeout for the source's dialect."""

    def __init__(self, src: Source, sql: str, params: dict):
        self.src, self.sql, self.params = src, sql, params
        self.timeout = src.cfg.statement_timeout_s
        self.timer: threading.Timer | None = None
        self.timed_out = False

    def __enter__(self):
        self.con = self.src.engine.connect()
        self.tx = self.con.begin()
        raw = self.con.connection.dbapi_connection
        try:
            if self.src.dialect == "postgres":
                self.con.execute(text("SET TRANSACTION READ ONLY"))
                self.con.execute(text(f"SET LOCAL statement_timeout = {int(self.timeout * 1000)}"))
            elif self.src.dialect == "sqlite":
                deadline = time.monotonic() + self.timeout
                raw.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
            elif self.src.dialect == "duckdb":
                self.timer = threading.Timer(self.timeout, self._interrupt, args=(raw,))
                self.timer.start()
            return self.con.execute(text(self.sql), self.params)
        except Exception as e:
            self.__exit__(type(e), e, None)
            if self.timed_out or "interrupt" in str(e).lower() or "statement timeout" in str(e).lower():
                raise QueryTimeout(f"query exceeded the {self.timeout:g}s statement timeout") from e
            raise

    def _interrupt(self, raw) -> None:
        self.timed_out = True
        raw.interrupt()

    def __exit__(self, *exc):
        if self.timer:
            self.timer.cancel()
        if self.src.dialect == "sqlite":
            try:
                self.con.connection.dbapi_connection.set_progress_handler(None, 0)
            except Exception:  # noqa: BLE001
                pass
        try:
            self.tx.rollback()  # read-only: never commit
        except Exception:  # noqa: BLE001 — connection may already be broken by an interrupt
            pass
        finally:
            self.con.close()
        e = exc[1]
        if e is not None and not isinstance(e, QueryTimeout) and (
                self.timed_out or "interrupt" in str(e).lower() or "statement timeout" in str(e).lower()):
            raise QueryTimeout(f"query exceeded the {self.timeout:g}s statement timeout") from e
        return False


def build_sources(cfg, guard: SqlasGuard | None = None) -> dict[str, Source]:
    """Instantiate every configured source (lazy: nothing connects until first use)."""
    from bankql.semantic import load_semantic

    guard = guard or SqlasGuard()
    out = {}
    for name, raw in cfg.sources.items():
        sc = SourceConfig.model_validate(raw)
        out[name] = Source(name, sc, load_semantic(sc.semantic), guard)
    return out
