import csv

from bankql.export import Exporter

Q = "SELECT trans_id, date, amount FROM trans WHERE date >= '1998-01-01' ORDER BY trans_id"


def test_csv_export_streams_full_result(src, tmp_path):
    ex = Exporter(tmp_path)
    e = ex.export(src, Q)
    rows = list(csv.reader(e.path.open()))
    total = src.run("SELECT COUNT(*) FROM trans WHERE date >= '1998-01-01'").rows[0][0]
    assert rows[0] == ["trans_id", "date", "amount"] and len(rows) - 1 == e.rows == total > 100
    assert ex.path_for(e.id) == e.path


def test_parquet_export(sqlite_src, tmp_path):
    import duckdb

    e = Exporter(tmp_path).export(sqlite_src, Q, fmt="parquet")
    assert duckdb.connect().execute(f"SELECT COUNT(*) FROM '{e.path}'").fetchone()[0] == e.rows


def test_formula_injection_neutralised(sqlite_src, tmp_path):
    e = Exporter(tmp_path).export(sqlite_src, "SELECT '=HYPERLINK(1)' AS a, '+1' AS b, 'ok' AS c")
    assert list(csv.reader(e.path.open()))[1] == ["'=HYPERLINK(1)", "'+1", "ok"]


def test_export_ids_cannot_traverse(tmp_path):
    ex = Exporter(tmp_path)
    assert ex.path_for("../../etc/passwd") is None and ex.path_for("0" * 32) is None


def test_export_respects_validator(sqlite_src, tmp_path):
    import pytest
    from bankql.connectors import QueryRejected

    with pytest.raises(QueryRejected):
        Exporter(tmp_path).export(sqlite_src, "SELECT * FROM trans")
