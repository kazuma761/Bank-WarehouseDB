"""Dev-mode DuckDB federation. The SQLite ⋈ Postgres join needs DuckDB's sqlite_scanner/postgres_scanner
extensions; this session's network blocks extensions.duckdb.org, so that test reports the blocker as a skip."""
import pytest

from bankql.connectors.federation import FederatedSource, FederationUnavailable, build_federations

JOIN = ("SELECT a.frequency, COUNT(*) AS n FROM financial.trans t JOIN bank_pg.account a ON a.account_id = t.account_id "
        "WHERE t.date >= '1998-12-01' GROUP BY a.frequency ORDER BY a.frequency")


def test_federated_validation(cfg, sources):
    fed = build_federations(cfg, sources)["fed_dev"]
    assert fed.validate(JOIN).ok
    assert fed.validate("SELECT COUNT(*) FROM trans").code == "table_not_allowed"  # must be member-qualified
    assert fed.validate("SELECT COUNT(*) FROM financial.trans").code == "unbounded_large_scan"
    assert fed.validate("SELECT full_name FROM bank_pg.client_profile").code == "pii_output"
    assert fed.validate("SELECT * FROM nowhere.account").code == "table_not_allowed"


def test_duckdb_member_attaches_natively(sources):
    fed = FederatedSource("f", {"bank_demo": sources["bank_demo"]})
    r = fed.run("SELECT COUNT(*) FROM bank_demo.trans t JOIN bank_demo.account a ON a.account_id = t.account_id "
                "WHERE t.date >= '1998-12-01'")
    direct = sources["bank_demo"].run("SELECT COUNT(*) FROM trans WHERE date >= '1998-12-01'")
    assert r.rows == direct.rows


def test_sqlite_joined_to_postgres(cfg, sources):
    fed = build_federations(cfg, sources)["fed_dev"]
    try:
        got = fed.run(JOIN).rows
    except FederationUnavailable as e:
        pytest.skip(f"BLOCKED: {e}")
    expected = sources["financial"].run(
        "SELECT a.frequency, COUNT(*) FROM trans t JOIN account a ON a.account_id = t.account_id "
        "WHERE t.date >= '1998-12-01' GROUP BY a.frequency ORDER BY a.frequency").rows
    assert got == expected
