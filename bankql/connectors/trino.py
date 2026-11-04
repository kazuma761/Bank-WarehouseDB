"""Production federation via Trino: one SQLAlchemy source whose allowlist holds `catalog.schema.table` names,
so a query can join e.g. `postgres.public.account` with `hive.bank.trans`. Needs `pip install "trino[sqlalchemy]"`
and a Trino role that can only SELECT (read-only is enforced by Trino access control, not by this client).

Config example (config/sources.yaml):
    trino_prod:
      url_env: BANKQL_TRINO_URL   # trino://bankql_ro@trino.internal:8443/postgres?http_scheme=https
      dialect: trino
      tables: [postgres.public.account, postgres.public.disp, hive.bank.trans]
      semantic: [semantic/financial.yaml]
      large_tables: {trans: {time_column: date}}
"""
from __future__ import annotations

from functools import cached_property

from sqlalchemy import text

from bankql.connectors import Source
from bankql.guardrails import Catalog, TableMeta


class TrinoSource(Source):
    @cached_property
    def catalog(self) -> Catalog:
        tables = {}
        with self.engine.connect() as con:
            for ref in self.cfg.tables:
                cat, schema, table = ref.lower().split(".")
                cols = {c: t for c, t in con.execute(text(
                    f'SELECT column_name, data_type FROM "{cat}".information_schema.columns '
                    "WHERE table_schema = :s AND table_name = :t ORDER BY ordinal_position"), {"s": schema, "t": table})}
                tables[(f"{cat}.{schema}", table)] = self._meta(table, cols, f"{cat}.{schema}")
        return Catalog(tables, default_schemas=frozenset())

    def _meta(self, table: str, cols: dict, qualifier: str) -> TableMeta:
        sem = self.semantic.table(table)
        lt = {k.lower(): v for k, v in self.cfg.large_tables.items()}.get(table)
        return TableMeta(table, cols, qualifier, {c.lower() for c, col in (sem.columns.items() if sem else []) if col.pii},
                         (lt.time_column if lt and lt.time_column else (sem.time_column if sem else None)), lt is not None)
