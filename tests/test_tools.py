import pytest

from bankql.tools import LookupTools, ToolError, template_sql
from tests.conftest import SOURCES


@pytest.fixture
def tools(src):
    return LookupTools(src)


def _norm(rows):
    return [[v.isoformat() if hasattr(v, "isoformat") else (round(v, 4) if isinstance(v, float) else v) for v in r] for r in rows]


@pytest.mark.parametrize("call", [
    ("get_client", {"client_id": 1}), ("get_accounts", {"client_id": 7}), ("get_cards", {"client_id": 1}),
    ("get_loans", {"client_id": 10}), ("get_orders", {"account_id": 3}),
    ("get_statement", {"account_id": 3}), ("get_statement", {"account_id": 3, "date_from": "1995-01-01", "date_to": "1998-06-30"}),
])
def test_tools_agree_across_dialects(sources, call):
    name, args = call
    got = {s: _norm(getattr(LookupTools(sources[s]), name)(**args).rows) for s in SOURCES}
    assert len({repr(v) for v in got.values()}) == 1, got


def test_get_client_includes_profile(tools):
    r = tools.get_client(1)
    row = dict(zip(r.columns, r.rows[0]))
    assert row["full_name"] == "Rahul Sharma" and row["phone_masked"].startswith("+91-XXXXXX-")


def test_get_accounts_shows_disposition(tools):
    r = tools.get_accounts(331)  # clients 331.. are disponents only
    assert r.rows and {dict(zip(r.columns, x))["disposition"] for x in r.rows} == {"DISPONENT"}
    assert "frequency" in r.legend


def test_statement_default_window_is_last_3_months(tools):
    r = tools.get_statement(3)
    assert r.params["date_to"] == "1998-12-31" and r.params["date_from"] == "1998-09-30"
    assert any("last 3 months" in n for n in r.notes)
    dates = [str(x[r.columns.index("date")])[:10] for x in r.rows]
    assert dates == sorted(dates) and all("1998-09-30" <= d <= "1998-12-31" for d in dates)
    assert r.legend["type"]["PRIJEM"] == "credit"


def test_loans_by_account_or_client(tools):
    assert tools.get_loans(account_id=tools.get_loans(client_id=10).rows[0][1]).rows if tools.get_loans(client_id=10).rows else True
    with pytest.raises(ToolError):
        tools.get_loans()


@pytest.mark.parametrize("bad", [
    lambda t: t.get_client("abc"), lambda t: t.get_client(-1), lambda t: t.get_statement(3, "1998-13-01"),
    lambda t: t.get_statement(3, "1998-12-01", "1998-01-01"),
])
def test_bad_arguments(tools, bad):
    with pytest.raises(ToolError):
        bad(tools)


def test_order_template_quoting_per_dialect():
    assert "`order`" in template_sql("get_orders", "mysql")
    assert "[order]" in template_sql("get_orders", "tsql")
    assert '"order"' in template_sql("get_orders", "sqlite") and ":account_id" in template_sql("get_orders", "duckdb")
