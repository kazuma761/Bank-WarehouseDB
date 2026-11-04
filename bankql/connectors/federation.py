"""Dev-mode federation: an in-memory DuckDB that ATTACHes member sources read-only, so one validated query can
join `financial.trans` (SQLite) with `bank_pg.account` (Postgres). Production federation: Trino (trino.py).

ATTACH of SQLite / Postgres / MySQL needs DuckDB's sqlite_scanner / postgres_scanner / mysql_scanner
extensions (installed from extensions.duckdb.org on first use). DuckDB files attach natively.
"""
from __future__ import annotations

from functools import cached_property

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine, make_url

from bankql.guardrails import Catalog, SqlValidator, TableMeta
from bankql.connectors import Source, SourceConfig

_EXT = {"sqlite": "sqlite_scanner", "postgres": "postgres_scanner", "mysql": "mysql_scanner"}


class FederationUnavailable(RuntimeError):
    pass


def _attach_sql(member: Source) -> tuple[str | None, str]:
    url = make_url(member.url)
    if member.dialect == "sqlite":
        return _EXT["sqlite"], f"ATTACH '{url.database}' AS {member.name} (TYPE sqlite, READ_ONLY)"
    if member.dialect == "duckdb":
        return None, f"ATTACH '{url.database}' AS {member.name} (READ_ONLY)"
    if member.dialect in ("postgres", "mysql"):
        q = url.query
        parts = {"host": url.host or "localhost", "port": url.port, "dbname" if member.dialect == "postgres" else "database": url.database,
                 "user": url.username, "password": url.password, **({"sslmode": q["sslmode"]} if "sslmode" in q else {})}
        dsn = " ".join(f"{k}={v}" for k, v in parts.items() if v is not None)
        return _EXT[member.dialect], f"ATTACH '{dsn}' AS {member.name} (TYPE {member.dialect}, READ_ONLY)"
    raise FederationUnavailable(f"member {member.name!r}: dialect {member.dialect} cannot be ATTACHed by DuckDB")


class FederatedSource(Source):
    def __init__(self, name: str, members: dict[str, Source], *, row_cap: int = 100_000,
                 statement_timeout_s: float = 30, large_scan_threshold_rows: int = 500_000):
        first = next(iter(members.values()))
        cfg = SourceConfig(url_env="", dialect="duckdb", tables=[], semantic=[], row_cap=row_cap,
                           statement_timeout_s=statement_timeout_s, large_scan_threshold_rows=large_scan_threshold_rows,
                           description=f"DuckDB federation of {', '.join(members)}")
        super().__init__(name, cfg, first.semantic, first.guard)
        self.members = members

    @cached_property
    def engine(self) -> Engine:
        attaches = [_attach_sql(m) for m in self.members.values()]
        exts = sorted({e for e, _ in attaches if e})
        eng = create_engine("duckdb:///:memory:")

        @event.listens_for(eng, "connect")
        def _attach(dbapi_con, _record):
            for ext in exts:
                try:
                    dbapi_con.execute(f"LOAD {ext}")
                except Exception as e:  # noqa: BLE001
                    raise FederationUnavailable(
                        f"DuckDB extension {ext} is not available ({str(e).splitlines()[0][:160]}). "
                        "Allow extensions.duckdb.org or pre-install the extension; see README 'Federation'.") from e
            for _, sql in attaches:
                dbapi_con.execute(sql)
        return eng

    @cached_property
    def catalog(self) -> Catalog:
        tables = {}
        for mname, m in self.members.items():
            for (_, tname), meta in m.catalog.tables.items():
                tables[(mname.lower(), tname)] = TableMeta(meta.name, meta.columns, mname, meta.pii, meta.time_column, meta.large)
        return Catalog(tables)

    @cached_property
    def validator(self) -> SqlValidator:
        return SqlValidator("duckdb", self.catalog, scan_threshold_rows=self.cfg.large_scan_threshold_rows,
                            estimate_rows=lambda meta: self.members[meta.qualifier].estimate_rows(meta),
                            as_of_hint=self._as_of_hint, sqlas=self.guard)

    def _as_of_hint(self) -> str:
        return min(m.as_of() for m in self.members.values()).isoformat()

    def as_of(self):
        return min(m.as_of() for m in self.members.values())

    def has_table(self, name: str) -> bool:
        return any(m.has_table(name) for m in self.members.values())


def build_federations(cfg, sources: dict[str, Source]) -> dict[str, FederatedSource]:
    return {name: FederatedSource(name, {m: sources[m] for m in f.members}, row_cap=f.row_cap,
                                  statement_timeout_s=f.statement_timeout_s,
                                  large_scan_threshold_rows=f.large_scan_threshold_rows)
            for name, f in cfg.federations.items()}
