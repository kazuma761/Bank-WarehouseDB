"""Phase 2 acceptance: the validator rejects DDL/DML (incl. inside CTEs and subqueries), multi-statement input
and unfiltered scans of large tables — on every dialect — while known-good SELECTs pass (positive controls)."""
import pytest

from bankql.connectors import QueryRejected
from bankql.guardrails import render, repair_reserved_tables
import sqlglot

GOOD = [
    "SELECT COUNT(*) AS n FROM account",
    'SELECT o.k_symbol, COUNT(*) AS n FROM "order" AS o GROUP BY o.k_symbol',
    "SELECT type, SUM(amount) FROM trans WHERE date >= '1998-10-01' GROUP BY type",
    "SELECT COUNT(*) FROM trans WHERE date BETWEEN '1998-01-01' AND '1998-12-31'",
    "WITH x AS (SELECT account_id FROM loan WHERE status = 'D') SELECT COUNT(*) FROM x",
    "SELECT account_id FROM loan WHERE account_id IN (SELECT account_id FROM \"order\" WHERE k_symbol = 'UVER')",
    "SELECT A2, A11 FROM district WHERE A3 = 'Prague' -- comment is stripped before execution",
    "SELECT COUNT(*) FROM account WHERE frequency LIKE '%please DROP TABLE account%'",  # keyword in a string: fine
    "SELECT district_id FROM client UNION SELECT district_id FROM account",
]

BAD = [
    ("DROP TABLE account", "not_select"),
    ("DELETE FROM account", "not_select"),
    ("UPDATE account SET frequency = 'x'", "not_select"),
    ("INSERT INTO account VALUES (1, 1, 'x', '1995-01-01')", "not_select"),
    ("CREATE TABLE t AS SELECT * FROM account", "not_select"),
    ("ALTER TABLE account ADD COLUMN x INT", "not_select"),
    ("WITH x AS (DELETE FROM account RETURNING *) SELECT * FROM x", "forbidden_node"),
    ("WITH x AS (UPDATE account SET frequency='x' RETURNING *) SELECT COUNT(*) FROM x", "forbidden_node"),
    ("SELECT * FROM (WITH d AS (INSERT INTO account VALUES (9,1,'x','1995-01-01') RETURNING *) SELECT * FROM d) s", "forbidden_node"),
    ("SELECT 1; DROP TABLE account", "multi_statement"),
    ("SELECT 1; SELECT 2", "multi_statement"),
    ("SELECT * INTO acct_copy FROM account", "forbidden_node"),
    ("SELECT * FROM account FOR UPDATE", "forbidden_node"),
    ("SELECT * FROM sqlite_master", "table_not_allowed"),
    ("SELECT * FROM information_schema.tables", "table_not_allowed"),
    ("SELECT * FROM pg_catalog.pg_roles", "table_not_allowed"),
    ("SELECT * FROM read_csv('/etc/passwd')", "forbidden_function"),
    ("SELECT pg_sleep(10)", "forbidden_function"),
    ("SELECT COUNT(*) FROM trans", "unbounded_large_scan"),
    ("SELECT * FROM trans WHERE amount > 100", "unbounded_large_scan"),
    ("SELECT t.* FROM trans t JOIN account a ON a.account_id = t.account_id WHERE a.date > '1995-01-01'", "unbounded_large_scan"),
    ("WITH x AS (SELECT * FROM trans) SELECT COUNT(*) FROM x WHERE x.date > '1998-01-01'", "unbounded_large_scan"),
    ("SELECT COUNT(*) FROM trans WHERE date IS NOT NULL", "unbounded_large_scan"),
    ("", "empty"),
]


@pytest.mark.parametrize("sql", GOOD)
def test_good_sql_passes_and_runs(src, sql):
    v = src.validate(sql)
    assert v.ok, (v.code, v.reason)
    src.run(sql, max_rows=5)  # and actually executes on this dialect


@pytest.mark.parametrize("sql,code", BAD)
def test_bad_sql_is_rejected(src, sql, code):
    v = src.validate(sql)
    assert not v.ok and v.sql is None
    assert v.code == code, (v.code, v.reason)


def test_rejected_sql_never_executes(src):
    with pytest.raises(QueryRejected):
        src.run("DROP TABLE account")
    assert src.run("SELECT COUNT(*) FROM account").rows[0][0] == 350  # still there


def test_large_scan_message_is_helpful(src):
    v = src.validate("SELECT COUNT(*) FROM trans")
    assert "trans.date" in v.reason and "1998-12-31" in v.reason and "EXPLAIN" in v.reason


def test_large_scan_threshold_is_configurable(sources):
    from bankql.connectors import Source

    s = sources["financial"]
    roomy = Source("roomy", s.cfg.model_copy(update={"large_scan_threshold_rows": 10_000}), s.semantic, s.guard)
    assert roomy.validate("SELECT COUNT(*) FROM trans").ok  # 3,590 rows < threshold: allowed


def test_render_keeps_bind_params_and_quotes_order():
    t = sqlglot.parse_one('SELECT o.amount FROM "order" AS o WHERE o.account_id = :a AND o.amount > :m', read="postgres")
    for d in ("postgres", "duckdb", "sqlite", "bigquery"):
        out = render(t, d)
        assert ":a" in out and ":m" in out, out
    assert "`order`" in render(t, "mysql") and "[order]" in render(t, "tsql") and '"order"' in render(t, "postgres")


def test_bare_reserved_table_is_quoted_on_output(src):
    v = src.validate("SELECT COUNT(*) FROM order")
    assert v.ok and ('"order"' in v.sql), v.sql
    assert src.run("SELECT COUNT(*) FROM order").rows[0][0] == 519


def test_repair_reserved_tables_regex():
    assert repair_reserved_tables("SELECT order.amount FROM order JOIN x ON 1=1 ORDER BY 1", ["order"]) == \
        'SELECT "order".amount FROM "order" JOIN x ON 1=1 ORDER BY 1'
