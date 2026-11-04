import sqlite3

from tests.conftest import SOURCES

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from bankql.connectors import QueryTimeout, Source, SourceConfig

SLOW = "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < 500000000) SELECT COUNT(*) FROM r"


def test_same_answers_on_every_dialect(sources):
    q = ('SELECT d.A3 AS region, COUNT(*) AS n FROM client c JOIN district d ON d.district_id = c.district_id '
         'GROUP BY d.A3 ORDER BY region')
    results = {name: sources[name].run(q).rows for name in SOURCES}
    assert len({repr(r) for r in results.values()}) == 1, results


def test_driver_level_read_only(src):
    """Even bypassing our validator, the connection itself refuses writes."""
    with src.engine.connect() as con, pytest.raises(Exception) as e:
        con.execute(text("CREATE TABLE should_not_exist (x INT)"))
    assert any(w in str(e.value).lower() for w in ("read-only", "readonly", "read only", "attempt to write")), e.value


def test_sqlite_opened_with_mode_ro(sqlite_src):
    raw = sqlite_src.engine.raw_connection()
    try:
        with pytest.raises(sqlite3.OperationalError):
            raw.execute("INSERT INTO district (district_id, A2, A3, A4, A5, A6, A7, A8, A9, A10, A11, A13, A14, A16) "
                        "VALUES (999, 'x', 'x', '1', '1', '1', '1', 1, 1, 1, 1, 1, 1, 1)")
    finally:
        raw.close()


def test_row_cap_truncates(src):
    r = src.run("SELECT client_id FROM client ORDER BY client_id", max_rows=10)
    assert len(r.rows) == 10 and r.truncated
    assert not src.run("SELECT COUNT(*) FROM client").truncated


def test_statement_timeout(sources, src):
    quick = Source("t", src.cfg.model_copy(update={"statement_timeout_s": 0.5}), src.semantic, src.guard)
    with pytest.raises(QueryTimeout):
        quick.run(SLOW)
    assert quick.run("SELECT COUNT(*) FROM client").rows[0][0] == 400  # connection still usable afterwards


def test_missing_env_var_is_a_clear_error(sources):
    s = sources["financial"]
    broken = Source("nope", s.cfg.model_copy(update={"url_env": "BANKQL_DOES_NOT_EXIST"}), s.semantic)
    with pytest.raises(RuntimeError, match="BANKQL_DOES_NOT_EXIST"):
        broken.run("SELECT 1")


def test_read_only_false_is_rejected():
    with pytest.raises(ValidationError):
        SourceConfig(url_env="X", dialect="sqlite", read_only=False, tables=["t"], semantic=[])


def test_describe_and_allowlist(src):
    d = src.describe_table("trans")
    assert d["large"] and d["time_column"] == "date"
    typ = next(c for c in d["columns"] if c["name"] == "type")
    assert typ["valid_values"]["PRIJEM"] == "credit"
    assert next(c for c in src.describe_table("client_profile")["columns"] if c["name"] == "full_name")["pii"]


def test_as_of_is_latest_transaction(src):
    assert src.as_of().isoformat() == "1998-12-31"


def test_concurrent_queries_more_threads_than_pool(src):
    """Regression: SingletonThreadPool on the SQLite creator engine segfaulted under concurrent use."""
    from concurrent.futures import ThreadPoolExecutor

    def q(i):
        return src.run(f"SELECT COUNT(*) FROM loan WHERE loan_id > {i % 7}").rows[0][0]
    with ThreadPoolExecutor(24) as ex:
        results = list(ex.map(q, range(300)))
    assert results[0] == src.run("SELECT COUNT(*) FROM loan WHERE loan_id > 0").rows[0][0] and len(results) == 300
