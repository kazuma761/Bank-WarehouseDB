"""bankql CLI.

    bankql check-source <name>     connectivity, read-only enforcement (inspected, never by writing), timeout, tables
    bankql init-source <name>      semantic YAML skeleton from the live schema (TODO descriptions to fill in)
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml
from sqlalchemy import text

from bankql.config import load_config
from bankql.connectors import QueryTimeout, Source, SourceConfig, build_sources
from bankql.semantic import SemanticModel

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
SLOW = "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 2000000000) SELECT COUNT(*) FROM r"
PII_HINT = re.compile(r"(name|email|e_mail|phone|mobile|address|addr|ssn|dob|birth|passport|pan|aadhaar|iban|card_?n)", re.I)


def _scalar(con, sql: str, params: dict | None = None):
    return con.execute(text(sql), params or {}).scalar()


def read_only_checks(src: Source) -> list[tuple[str, str, str]]:
    """Inspect the connection's read-only guarantees per dialect. Never attempts a write."""
    out = []
    with src.engine.connect() as con:
        if src.dialect == "sqlite":
            q = _scalar(con, "PRAGMA query_only")
            out.append((PASS if q == 1 else FAIL, "sqlite query_only", f"PRAGMA query_only = {q} (file opened with mode=ro)"))
        elif src.dialect == "duckdb":
            mode = _scalar(con, "SELECT current_setting('access_mode')")
            out.append((PASS if str(mode).lower() == "read_only" else FAIL, "duckdb access_mode", f"access_mode = {mode}"))
        elif src.dialect == "postgres":
            role = con.execute(text("SELECT current_user, rolsuper, rolcreaterole, rolcreatedb, rolbypassrls "
                                    "FROM pg_roles WHERE rolname = current_user")).one()
            elevated = [n for n, v in zip(("superuser", "createrole", "createdb", "bypassrls"), role[1:]) if v]
            out.append((FAIL if elevated else PASS, "postgres role attributes",
                        f"role {role[0]}: " + (", ".join(elevated) if elevated else "no superuser/createrole/createdb/bypassrls")))
            # Role-level (server-side) setting: protects these credentials outside BankQL too. SHOW would report the
            # session value, which BankQL itself sets via connect options.
            cfgs = con.execute(text("SELECT unnest(setconfig) FROM pg_db_role_setting s JOIN pg_roles r ON r.oid = s.setrole "
                                    "WHERE r.rolname = current_user")).scalars().all()
            role_ro = "default_transaction_read_only=on" in cfgs
            out.append((PASS if role_ro else FAIL, "postgres default_transaction_read_only",
                        f"role-level: {'on' if role_ro else 'NOT SET'}; session: {_scalar(con, 'SHOW default_transaction_read_only')}"))
            writable = [m.name for m in src.catalog.tables.values()
                        if _scalar(con, "SELECT has_table_privilege(current_user, :t, 'INSERT,UPDATE,DELETE,TRUNCATE')",
                                   {"t": f'"{m.name}"'})]
            out.append((FAIL if writable else PASS, "postgres table write privileges",
                        f"write privilege on: {', '.join(writable)}" if writable else "no INSERT/UPDATE/DELETE/TRUNCATE on any allowlisted table"))
            create = [s for s in ("public",) if _scalar(con, "SELECT has_schema_privilege(current_user, :s, 'CREATE')", {"s": s})]
            out.append((FAIL if create else PASS, "postgres schema CREATE", "CREATE on public" if create else "no CREATE on public"))
            out.append((PASS, "postgres statement_timeout (role/session)", str(_scalar(con, "SHOW statement_timeout"))))
        else:
            out.append((WARN, f"{src.dialect} read-only", "not inspected automatically for this dialect: confirm the "
                        "database user has SELECT-only grants (docs/CONNECT.md part 2)"))
    return out


def check_source(src: Source) -> list[tuple[str, str, str]]:
    res: list[tuple[str, str, str]] = []
    try:
        with src.engine.connect() as con:
            _scalar(con, "SELECT 1")
        res.append((PASS, "connect", f"{src.dialect} via ${src.cfg.url_env}"))
    except Exception as e:  # noqa: BLE001
        return [(FAIL, "connect", f"{type(e).__name__}: {str(e).splitlines()[0][:200]}")]
    try:
        res += read_only_checks(src)
    except Exception as e:  # noqa: BLE001
        res.append((FAIL, "read-only inspection", f"{type(e).__name__}: {str(e)[:200]}"))
    try:
        present = {k[1] for k in src.catalog.tables}
        missing = [t for t in src.cfg.tables if t.lower() not in present]
        optional = [t for t in missing if (src.semantic.table(t) or SemanticModel(name="x").table(t)) and src.semantic.table(t).optional]
        hard = [t for t in missing if t not in optional]
        res.append((FAIL if hard else PASS, "allowlisted tables",
                    f"{len(present)} present" + (f"; missing: {', '.join(hard)}" if hard else "")
                    + (f"; optional absent: {', '.join(optional)}" if optional else "")))
        for meta in src.catalog.tables.values():
            if meta.large:
                ok = meta.time_column and meta.time_column in meta.columns
                res.append((PASS if ok else FAIL, f"large table {meta.name}",
                            f"time_column {meta.time_column!r} " + ("exists" if ok else "NOT FOUND")
                            + f"; ~{src.estimate_rows(meta):,} rows; guard threshold {src.cfg.large_scan_threshold_rows:,}"))
    except Exception as e:  # noqa: BLE001
        res.append((FAIL, "allowlisted tables", f"{type(e).__name__}: {str(e)[:200]}"))
    quick = Source(src.name, src.cfg.model_copy(update={"statement_timeout_s": 0.5}), src.semantic, src.guard)
    try:
        import time

        t0 = time.perf_counter()
        quick.run(SLOW)
        res.append((FAIL, "statement timeout", "a slow read query was NOT interrupted at 0.5 s"))
    except QueryTimeout:
        res.append((PASS, "statement timeout", f"slow read interrupted after {time.perf_counter() - t0:.2f}s "
                                               f"(configured {src.cfg.statement_timeout_s:g}s)"))
    except Exception as e:  # noqa: BLE001
        res.append((WARN, "statement timeout", f"could not test ({type(e).__name__}: {str(e)[:120]})"))
    return res


def init_source(src: Source, out: Path, *, sample_distinct: int = 12) -> Path:
    """Semantic YAML skeleton from the live schema. Suspected PII columns are flagged and never sampled."""
    reflected = src._reflect()
    wanted = {t.lower() for t in src.cfg.tables} or {t.lower() for t in reflected}
    doc: dict = {"name": src.name, "description": "TODO: what this database holds", "tables": {}, "joins": [], "glossary": {}}
    with src.engine.connect() as con:
        for tname, cols in sorted(reflected.items()):
            if tname.lower() not in wanted:
                continue
            tab: dict = {"description": "TODO", "columns": {}}
            for c, typ in cols.items():
                col: dict = {"description": "TODO"}
                if PII_HINT.search(c):
                    col["pii"] = True  # TODO confirm: suspected PII from the column name
                elif re.search(r"char|text|string|varchar", typ, re.I):
                    q = f'SELECT DISTINCT "{c}" FROM "{tname}" WHERE "{c}" IS NOT NULL LIMIT {sample_distinct + 1}'
                    try:
                        vals = [r[0] for r in con.execute(text(q))]
                    except Exception:  # noqa: BLE001
                        vals = []
                    if 0 < len(vals) <= sample_distinct:
                        col["valid_values"] = {str(v): "TODO meaning" for v in vals}
                if re.search(r"date|time", typ, re.I) and "time_column" not in tab:
                    tab["time_column"] = c  # TODO confirm; mark the table large in sources.yaml if it is a fact table
                tab["columns"][c] = col
            doc["tables"][tname] = tab
    if src.dialect != "duckdb":  # duckdb_engine's SQLAlchemy reflection breaks on DuckDB 2.0
        from sqlalchemy import inspect

        insp = inspect(src.engine)
        for t in doc["tables"]:
            for fk in insp.get_foreign_keys(t):
                for a, b in zip(fk["constrained_columns"], fk["referred_columns"]):
                    doc["joins"].append([f"{t}.{a}", f"{fk['referred_table']}.{b}"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("# GENERATED by `bankql init-source` — replace every TODO; confirm pii and time_column.\n"
                   + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="bankql")
    ap.add_argument("--config", default="config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check-source", help="verify a source: connectivity, read-only, timeout, tables")
    c.add_argument("name")
    i = sub.add_parser("init-source", help="write a semantic YAML skeleton from the live schema")
    i.add_argument("name")
    i.add_argument("-o", "--out", help="default: config/semantic/<name>.yaml")
    i.add_argument("--force", action="store_true", help="overwrite an existing file")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    if a.name not in cfg.sources:
        print(f"unknown source {a.name!r}; configured: {', '.join(cfg.sources)}", file=sys.stderr)
        return 2
    if a.cmd == "check-source":
        src = build_sources(cfg)[a.name]
        results = check_source(src)
        for status, what, detail in results:
            print(f"{status}  {what:42} {detail}")
        return 1 if any(s == FAIL for s, _, _ in results) else 0
    raw = dict(cfg.sources[a.name])
    raw["semantic"] = []  # skeleton generation must not depend on a semantic file that may not exist yet
    src = Source(a.name, SourceConfig.model_validate(raw), SemanticModel(name=a.name))
    out = Path(a.out) if a.out else Path(a.config) / "semantic" / f"{a.name}.yaml"
    if out.exists() and not a.force:
        print(f"{out} exists; use --force to overwrite", file=sys.stderr)
        return 1
    print(init_source(src, out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
