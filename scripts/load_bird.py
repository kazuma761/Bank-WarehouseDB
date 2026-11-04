"""Load BIRD mini-dev "financial" into DuckDB (+ synthetic client_profile) and PostgreSQL; leave the original SQLite
untouched for evaluation. Verifies every financial gold SQL executes and that row counts match in all three.

    .venv/bin/python scripts/load_bird.py --bird-dir data/bird           # after unzipping minidev.zip there
Writes BANKQL_BIRD_URL / BANKQL_BIRD_DEMO_URL to .env (BANKQL_BIRD_PG_URL comes from scripts/setup_postgres.sh).
"""
from __future__ import annotations

import argparse
import os
import random
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bankql.config import load_dotenv  # noqa: E402
from bankql.eval.bird import find, load_bird  # noqa: E402
from scripts.make_fixture import DDL, PROFILE_DDL, TABLES, _ddl_8byte_floats, _pg_dsn  # noqa: E402

DEMO_PATH = ROOT / "data/bird_demo.duckdb"
BATCH = 50_000


def _set_env(key: str, value: str) -> None:
    env = ROOT / ".env"
    lines = [l for l in (env.read_text().splitlines() if env.exists() else []) if not l.startswith(f"{key}=")]
    env.write_text("\n".join(lines + [f"{key}={value}"]) + "\n")
    os.chmod(env, 0o600)
    os.environ[key] = value


def _columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]


def _expected_columns(table: str) -> list[str]:
    import sqlglot

    stmt = next(d for d in DDL if sqlglot.parse_one(d).find(sqlglot.exp.Table).name == table)
    return [c.name for c in sqlglot.parse_one(stmt).find_all(sqlglot.exp.ColumnDef)]


def check_schema(src: sqlite3.Connection) -> None:
    for t in TABLES:
        got, want = _columns(src, t), _expected_columns(t)
        if [c.lower() for c in got] != [c.lower() for c in want]:
            raise SystemExit(f"BIRD table {t} has columns {got}, expected {want} — update scripts/make_fixture.DDL")


def profiles(client_ids: list[int], seed: int = 42) -> list[tuple]:
    """Synthetic Indian names (Faker en_IN) for every BIRD client, with planted collisions for ambiguity tests."""
    from faker import Faker

    fake, rng = Faker("en_IN"), random.Random(seed)
    fake.seed_instance(seed)
    rows = [(cid, fake.name(), fake.city(), f"+91-XXXXXX-{rng.randint(1000, 9999)}") for cid in client_ids]
    for i, j in ((0, 1), (2, 3), (4, 5)):  # three planted exact-name pairs
        if j < len(rows):
            rows[j] = (rows[j][0], rows[i][1]) + rows[j][2:]
    return rows


def copy_duckdb(src: sqlite3.Connection, prof: list[tuple], path: Path = DEMO_PATH) -> None:
    import duckdb

    path.unlink(missing_ok=True)
    con = duckdb.connect(str(path))
    for stmt in _ddl_8byte_floats(DDL + [PROFILE_DDL]):
        con.execute(stmt)
    for t in TABLES:
        cols = _columns(src, t)
        cur = src.execute(f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)} FROM "{t}"')
        while rows := cur.fetchmany(BATCH):
            con.executemany(f'INSERT INTO "{t}" VALUES ({", ".join("?" * len(cols))})', rows)
    con.executemany("INSERT INTO client_profile VALUES (?, ?, ?, ?)", prof)
    con.close()


def copy_postgres(src: sqlite3.Connection, prof: list[tuple], admin_url: str) -> None:
    import psycopg

    with psycopg.connect(_pg_dsn(admin_url)) as con, con.cursor() as cur:
        for t in reversed(TABLES + ["client_profile"]):
            cur.execute(f'DROP TABLE IF EXISTS "{t}" CASCADE')
        for stmt in _ddl_8byte_floats(DDL + [PROFILE_DDL]):
            cur.execute(stmt)
        for t in TABLES:
            cols = _columns(src, t)
            rows = src.execute(f'SELECT {", ".join(chr(34) + c + chr(34) for c in cols)} FROM "{t}"')
            with cur.copy(f'COPY "{t}" FROM STDIN') as cp:
                for r in rows:
                    cp.write_row(r)
        with cur.copy('COPY client_profile FROM STDIN') as cp:
            for r in prof:
                cp.write_row(r)


def verify(sqlite_path: Path, pg_url: str | None) -> dict:
    import duckdb

    lite = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    duck = duckdb.connect(str(DEMO_PATH), read_only=True)
    pg = None
    if pg_url:
        import psycopg

        pg = psycopg.connect(_pg_dsn(pg_url))
    counts = {}
    for t in TABLES:
        q = f'SELECT COUNT(*) FROM "{t}"'
        c = [lite.execute(q).fetchone()[0], duck.execute(q).fetchone()[0]] + ([pg.execute(q).fetchone()[0]] if pg else [])
        assert len(set(c)) == 1, f"{t}: row counts differ {c}"
        counts[t] = c[0]
    return counts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bird-dir", default=str(ROOT / "data/bird"))
    ap.add_argument("--no-postgres", action="store_true")
    a = ap.parse_args()
    load_dotenv(ROOT / ".env")
    sqlite_path = find(a.bird_dir, "financial.sqlite")
    items, _, broken = load_bird(a.bird_dir)
    print(f"BIRD financial: {sqlite_path}; {len(items)} questions with executable gold SQL; {len(broken)} broken: "
          f"{[b['id'] for b in broken]}")
    src = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)  # the original is never written
    check_schema(src)
    prof = profiles([r[0] for r in src.execute("SELECT client_id FROM client ORDER BY client_id")])
    copy_duckdb(src, prof)
    pg_admin = None if a.no_postgres else os.environ.get("BANKQL_BIRD_PG_ADMIN_URL")
    if pg_admin:
        copy_postgres(src, prof, pg_admin)
    elif not a.no_postgres:
        print("BANKQL_BIRD_PG_ADMIN_URL not set — run scripts/setup_postgres.sh first; skipping Postgres")
    _set_env("BANKQL_BIRD_URL", f"sqlite:///{sqlite_path.resolve()}")
    _set_env("BANKQL_BIRD_DEMO_URL", f"duckdb:///{DEMO_PATH}")
    counts = verify(sqlite_path, os.environ.get("BANKQL_BIRD_PG_URL") if pg_admin else None)
    print("row counts match:", counts)


if __name__ == "__main__":
    main()
