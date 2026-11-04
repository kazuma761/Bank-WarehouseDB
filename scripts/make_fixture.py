"""Deterministic synthetic fixture with BIRD "financial"'s schema (Berka/PKDD'99 layout).

Writes data/fixture/financial.sqlite (8 BIRD tables, like the original), data/bank_demo.duckdb
(8 tables + synthetic client_profile) and loads the 8 tables into Postgres (BANKQL_PG_ADMIN_URL).
All values are synthetic. Used by every test until the real BIRD data arrives.

    .venv/bin/python scripts/make_fixture.py            # build all three
    .venv/bin/python scripts/make_fixture.py --check    # verify row counts match
"""
from __future__ import annotations

import os
import random
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SQLITE_PATH = ROOT / "data/fixture/financial.sqlite"
DUCKDB_PATH = ROOT / "data/bank_demo.duckdb"
SEED = 42
AS_OF = date(1998, 12, 31)  # latest trans.date, like BIRD's data window (1993–1998)

# DDL mirrors BIRD financial.sqlite (types, nullability, FKs). "order" is a reserved word: always quoted.
DDL = [
    """CREATE TABLE district (district_id INTEGER DEFAULT 0 NOT NULL PRIMARY KEY, A2 TEXT NOT NULL, A3 TEXT NOT NULL,
       A4 TEXT NOT NULL, A5 TEXT NOT NULL, A6 TEXT NOT NULL, A7 TEXT NOT NULL, A8 INTEGER NOT NULL, A9 INTEGER NOT NULL,
       A10 REAL NOT NULL, A11 INTEGER NOT NULL, A12 REAL NULL, A13 REAL NOT NULL, A14 INTEGER NOT NULL,
       A15 INTEGER NULL, A16 INTEGER NOT NULL)""",
    """CREATE TABLE client (client_id INTEGER NOT NULL PRIMARY KEY, gender TEXT NOT NULL, birth_date DATE NOT NULL,
       district_id INTEGER NOT NULL REFERENCES district (district_id))""",
    """CREATE TABLE account (account_id INTEGER DEFAULT 0 NOT NULL PRIMARY KEY,
       district_id INTEGER DEFAULT 0 NOT NULL REFERENCES district (district_id), frequency TEXT NOT NULL, date DATE NOT NULL)""",
    """CREATE TABLE disp (disp_id INTEGER NOT NULL PRIMARY KEY, client_id INTEGER NOT NULL REFERENCES client (client_id),
       account_id INTEGER NOT NULL REFERENCES account (account_id), type TEXT NOT NULL)""",
    """CREATE TABLE card (card_id INTEGER DEFAULT 0 NOT NULL PRIMARY KEY, disp_id INTEGER NOT NULL REFERENCES disp (disp_id),
       type TEXT NOT NULL, issued DATE NOT NULL)""",
    """CREATE TABLE loan (loan_id INTEGER DEFAULT 0 NOT NULL PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES account (account_id),
       date DATE NOT NULL, amount INTEGER NOT NULL, duration INTEGER NOT NULL, payments REAL NOT NULL, status TEXT NOT NULL)""",
    """CREATE TABLE "order" (order_id INTEGER DEFAULT 0 NOT NULL PRIMARY KEY, account_id INTEGER NOT NULL REFERENCES account (account_id),
       bank_to TEXT NOT NULL, account_to INTEGER NOT NULL, amount REAL NOT NULL, k_symbol TEXT NOT NULL)""",
    """CREATE TABLE trans (trans_id INTEGER DEFAULT 0 NOT NULL PRIMARY KEY,
       account_id INTEGER DEFAULT 0 NOT NULL REFERENCES account (account_id), date DATE NOT NULL, type TEXT NOT NULL,
       operation TEXT NULL, amount INTEGER NOT NULL, balance INTEGER NOT NULL, k_symbol TEXT NULL, bank TEXT NULL, account INTEGER NULL)""",
]
PROFILE_DDL = """CREATE TABLE client_profile (client_id INTEGER NOT NULL PRIMARY KEY REFERENCES client (client_id),
    full_name TEXT NOT NULL, city TEXT NOT NULL, phone_masked TEXT NOT NULL)"""
TABLES = ["district", "client", "account", "disp", "card", "loan", "order", "trans"]  # FK-safe load order

REGIONS = ["Prague", "central Bohemia", "south Bohemia", "west Bohemia", "north Bohemia",
           "east Bohemia", "south Moravia", "north Moravia"]
FREQUENCIES = ["POPLATEK MESICNE", "POPLATEK TYDNE", "POPLATEK PO OBRATU"]
# (type, operation, k_symbol choices)
TRANS_KINDS = [
    ("PRIJEM", "VKLAD", [None, ""]),
    ("PRIJEM", "PREVOD Z UCTU", ["DUCHOD", None]),
    ("PRIJEM", None, ["UROK"]),
    ("VYDAJ", "VYBER", [None, ""]),
    ("VYDAJ", "PREVOD NA UCET", ["SIPO", "UVER", "POJISTNE", "LEASING"]),
    ("VYDAJ", "VYBER KARTOU", [None]),
    ("VYDAJ", "VYBER", ["SLUZBY", "SANKC. UROK"]),
    ("VYBER", "VYBER", [None]),
]
# Planted name collisions so the resolver's ambiguity paths are testable.
PLANTED_NAMES = {1: "Rahul Sharma", 2: "Rahul Sharma", 3: "Priya Patel", 4: "Priya Patil", 5: "Ananya Iyer"}


def _rand_date(rng: random.Random, start: date, end: date) -> date:
    return start + timedelta(days=rng.randint(0, max(0, (end - start).days)))


def generate() -> dict[str, list[tuple]]:
    from faker import Faker

    rng = random.Random(SEED)
    fake_cz, fake_in = Faker("cs_CZ"), Faker("en_IN")
    fake_cz.seed_instance(SEED)
    fake_in.seed_instance(SEED)
    data: dict[str, list[tuple]] = {t: [] for t in TABLES + ["client_profile"]}

    names: set[str] = set()
    for d in range(1, 78):  # BIRD has 77 districts
        name = fake_cz.city()
        while name in names:
            name = fake_cz.city()
        names.add(name)
        inhabitants = rng.randint(40_000, 1_200_000)
        data["district"].append((
            d, name, REGIONS[0] if d == 1 else rng.choice(REGIONS[1:]), str(inhabitants),
            str(rng.randint(0, 150)), str(rng.randint(0, 70)), str(rng.randint(0, 20)), rng.randint(0, 10),
            rng.randint(1, 15), round(rng.uniform(30, 100), 1), rng.randint(8000, 12600),
            None if d % 30 == 0 else round(rng.uniform(0.2, 7.5), 2), round(rng.uniform(0.4, 9.5), 2),
            rng.randint(80, 170), None if d % 25 == 0 else rng.randint(800, 85_000), rng.randint(800, 99_000)))

    n_clients, n_accounts = 400, 350
    for c in range(1, n_clients + 1):
        data["client"].append((c, rng.choice("MF"), _rand_date(rng, date(1920, 1, 1), date(1980, 12, 31)),
                               rng.randint(1, 77)))
        name = PLANTED_NAMES.get(c) or fake_in.name()
        data["client_profile"].append((c, name, fake_in.city(), f"+91-XXXXXX-{rng.randint(1000, 9999)}"))

    owners = list(range(1, 331))  # clients 1..330 own accounts; 20 of them own a second account
    two_accounts = set(rng.sample(owners, 20))
    owner_of: list[int] = owners + sorted(two_accounts)
    disponents = list(range(331, n_clients + 1))  # 70 clients are only DISPONENT on someone's account
    disp_id = 0
    acct_open: dict[int, date] = {}
    for a, owner in enumerate(owner_of, start=1):
        opened = _rand_date(rng, date(1993, 1, 1), date(1997, 12, 31))
        acct_open[a] = opened
        data["account"].append((a, rng.randint(1, 77), rng.choice(FREQUENCIES), opened))
        disp_id += 1
        data["disp"].append((disp_id, owner, a, "OWNER"))
    for client, a in zip(disponents, rng.sample(range(1, n_accounts + 1), len(disponents))):
        disp_id += 1
        data["disp"].append((disp_id, client, a, "DISPONENT"))

    for card_id, d in enumerate(rng.sample(data["disp"], 90), start=1):
        data["card"].append((card_id, d[0], rng.choices(["classic", "junior", "gold"], [6, 2, 2])[0],
                             _rand_date(rng, acct_open[d[2]], AS_OF)))

    for loan_id, a in enumerate(sorted(rng.sample(range(1, n_accounts + 1), 70)), start=1):
        amount, duration = rng.randint(5, 600) * 1000, rng.choice([12, 24, 36, 48, 60])
        data["loan"].append((loan_id, a, _rand_date(rng, acct_open[a], date(1998, 12, 1)), amount, duration,
                             round(amount / duration, 1), rng.choices("ABCD", [3, 1, 5, 1])[0]))

    order_id = 0
    for a in range(1, n_accounts + 1):
        for _ in range(rng.choice([0, 1, 1, 2, 3])):
            order_id += 1
            data["order"].append((order_id, a, rng.choice(["AB", "CD", "EF", "GH", "IJ", "KL", "MN", "OP", "QR", "ST"]),
                                  rng.randint(10_000_000, 99_999_999), round(rng.uniform(50, 12_000), 1),
                                  rng.choice(["SIPO", "UVER", "POJISTNE", "LEASING", " "])))

    trans_id = 0
    for a in range(1, n_accounts + 1):
        balance = rng.randint(500, 3000)
        n = rng.randint(4, 16)
        # Half of each account's history lands in the last quarter so "last 3 months" windows are non-trivial.
        dates = sorted([_rand_date(rng, acct_open[a], AS_OF) for _ in range(n // 2)] +
                       [_rand_date(rng, date(1998, 10, 1), AS_OF) for _ in range(n - n // 2)])
        for i, dt in enumerate(dates):
            ttype, op, ks = (TRANS_KINDS[0][0], TRANS_KINDS[0][1], TRANS_KINDS[0][2]) if i == 0 else rng.choice(TRANS_KINDS)
            amount = rng.randint(100, 25_000) if ttype == "PRIJEM" else rng.randint(10, max(10, min(balance, 15_000)))
            balance = balance + amount if ttype == "PRIJEM" else balance - amount
            transfer = op in ("PREVOD Z UCTU", "PREVOD NA UCET")
            trans_id += 1
            data["trans"].append((trans_id, a, dt, ttype, op, amount, balance, rng.choice(ks),
                                  rng.choice(["AB", "CD", "EF", "GH"]) if transfer else None,
                                  rng.randint(10_000_000, 99_999_999) if transfer else None))
    return data


def _placeholders(n: int, style: str) -> str:
    return ", ".join(["?"] * n if style == "qmark" else ["%s"] * n)


def write_sqlite(data: dict[str, list[tuple]], path: Path = SQLITE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    con = sqlite3.connect(path)
    with con:
        for stmt in DDL:
            con.execute(stmt)
        for t in TABLES:
            rows = [tuple(v.isoformat() if isinstance(v, date) else v for v in r) for r in data[t]]
            con.executemany(f'INSERT INTO "{t}" VALUES ({_placeholders(len(rows[0]), "qmark")})', rows)
    con.close()


def write_duckdb(data: dict[str, list[tuple]], path: Path = DUCKDB_PATH) -> None:
    import duckdb

    path.parent.mkdir(parents=True, exist_ok=True)
    path.unlink(missing_ok=True)
    con = duckdb.connect(str(path))
    for stmt in DDL + [PROFILE_DDL]:
        con.execute(stmt)
    for t in TABLES + ["client_profile"]:
        con.executemany(f'INSERT INTO "{t}" VALUES ({_placeholders(len(data[t][0]), "qmark")})', data[t])
    con.close()


def write_postgres(data: dict[str, list[tuple]], admin_url: str) -> None:
    import psycopg

    with psycopg.connect(admin_url) as con, con.cursor() as cur:
        for t in reversed(TABLES):
            cur.execute(f'DROP TABLE IF EXISTS "{t}" CASCADE')
        for stmt in DDL:
            cur.execute(stmt)
        for t in TABLES:
            with cur.copy(f'COPY "{t}" FROM STDIN') as cp:
                for r in data[t]:
                    cp.write_row(r)


def _pg_dsn(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://")


def check_fixture() -> None:
    """Assert identical per-table row counts across SQLite, DuckDB and Postgres (read-only connections)."""
    import duckdb
    import psycopg

    sys.path.insert(0, str(ROOT))
    from bankql.config import load_dotenv

    load_dotenv(ROOT / ".env")
    lite = sqlite3.connect(f"file:{SQLITE_PATH}?mode=ro", uri=True)
    duck = duckdb.connect(str(DUCKDB_PATH), read_only=True)
    pg = psycopg.connect(_pg_dsn(os.environ["BANKQL_PG_URL"]))
    counts = {}
    for t in TABLES:
        q = f'SELECT COUNT(*) FROM "{t}"'
        c = (lite.execute(q).fetchone()[0], duck.execute(q).fetchone()[0], pg.execute(q).fetchone()[0])
        assert c[0] > 0 and len(set(c)) == 1, f"{t}: sqlite/duckdb/postgres counts differ {c}"
        counts[t] = c[0]
    assert duck.execute("SELECT COUNT(*) FROM client_profile").fetchone()[0] == counts["client"]
    cols = [r[1] for r in lite.execute("PRAGMA table_info(district)")]
    assert cols == ["district_id"] + [f"A{i}" for i in range(2, 17)], cols
    for con in (lite, duck, pg):
        codes = {r[0] for r in con.execute("SELECT DISTINCT type FROM trans").fetchall()}
        assert {"PRIJEM", "VYDAJ"} <= codes, codes
    try:  # read-only really is read-only on the role we ship
        pg.execute("CREATE TABLE should_fail (x int)")
        raise AssertionError("bankql_ro could create a table")
    except psycopg.errors.ReadOnlySqlTransaction:
        pass
    print("row counts match:", counts, "| total rows:", sum(counts.values()) + counts["client"])


def main() -> None:
    sys.path.insert(0, str(ROOT))
    from bankql.config import load_dotenv

    load_dotenv(ROOT / ".env")
    if "--check" not in sys.argv:
        data = generate()
        write_sqlite(data)
        write_duckdb(data)
        write_postgres(data, _pg_dsn(os.environ["BANKQL_PG_ADMIN_URL"]))
        print("fixture written:", {t: len(v) for t, v in data.items()})
    check_fixture()


if __name__ == "__main__":
    main()
