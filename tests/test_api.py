"""REST API end to end on FakeLLM."""
import json

import pytest
from fastapi.testclient import TestClient

from bankql.api import create_app
from tests.fakes import agent_fake, route

KEY = "test-key-123"
H = {"X-API-Key": KEY}
DEBT_SQL = "SELECT COUNT(*) AS n FROM loan WHERE status = 'D'"


@pytest.fixture
def client(make_agent, monkeypatch):
    monkeypatch.setenv("BANKQL_API_KEYS", f"other-key,{KEY}")
    agent = make_agent(agent_fake({"client 12": route(tools=[{"tool": "get_accounts", "client_id": 12}]),
                                   "in debt": route("analytics")}, summary="Some loans are in debt."),
                       {"in debt": DEBT_SQL})
    return TestClient(create_app(agent))


def test_health_needs_no_key_and_reveals_nothing(client):
    assert client.get("/health").json() == {"status": "ok"}


@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}, {"Authorization": "Bearer wrong"}, {"X-API-Key": ""}])
def test_auth_required(client, headers):
    assert client.post("/v1/ask", json={"question": "x"}, headers=headers).status_code == 401


def test_no_keys_configured_fails_closed(make_agent, monkeypatch):
    monkeypatch.setenv("BANKQL_API_KEYS", "")
    c = TestClient(create_app(make_agent(agent_fake({}))))
    assert c.get("/v1/sources", headers=H).status_code == 401


def test_bearer_auth_works(client):
    assert client.get("/v1/sources", headers={"Authorization": f"Bearer {KEY}"}).status_code == 200


def test_ask_lookup(client):
    r = client.post("/v1/ask", json={"question": "show client 12's accounts", "source": "bank_pg"}, headers=H).json()
    assert r["status"] == "ok" and r["route"] == "lookup" and r["rows"]


def test_ask_analytics_and_download(client, sources):
    r = client.post("/v1/ask", json={"question": "How many loans are in debt?", "source": "financial"}, headers=H).json()
    assert r["status"] == "ok" and r["rows"] == sources["financial"].run(DEBT_SQL).rows
    csv = client.get(f"/v1/results/{r['export_id']}.csv", headers=H)
    assert csv.status_code == 200 and csv.text.splitlines()[0] == "n"
    assert client.get(f"/v1/results/{r['export_id']}.csv").status_code == 401
    assert client.get("/v1/results/" + "0" * 32 + ".csv", headers=H).status_code == 404


def test_ask_sse_stream(client):
    with client.stream("POST", "/v1/ask", json={"question": "show client 12's accounts", "stream": True}, headers=H) as s:
        body = "".join(s.iter_text())
    events = [line.split(": ", 1)[1] for line in body.splitlines() if line.startswith("event: ")]
    assert events[0] == "stage" and events[-1] == "result"
    final = json.loads(body.strip().split("data: ")[-1])
    assert final["status"] == "ok"


def test_sources_and_tables(client):
    names = {s["name"] for s in client.get("/v1/sources", headers=H).json()}
    assert {"financial", "bank_demo", "bank_pg", "fed_dev"} <= names
    tables = client.get("/v1/sources/financial/tables", headers=H).json()
    assert {t["table"] for t in tables} >= {"trans", "order", "client_profile"}
    assert client.get("/v1/sources/nope/tables", headers=H).status_code == 404


def test_sql_validate(client):
    ok = client.post("/v1/sql/validate", json={"source": "financial", "sql": "SELECT COUNT(*) FROM account"}, headers=H).json()
    bad = client.post("/v1/sql/validate", json={"source": "financial", "sql": "DELETE FROM account"}, headers=H).json()
    assert ok["ok"] and not bad["ok"] and bad["code"] == "not_select"


def test_sql_run_off_by_default(client):
    r = client.post("/v1/sql/run", json={"source": "bank_demo", "sql": "SELECT 1"}, headers=H)
    assert r.status_code == 403 and "run_sql" in r.json()["detail"]


def test_sql_run_is_guarded_for_enabled_key(make_agent, tmp_path, monkeypatch):
    from bankql.auth import KeyStore, hash_key

    monkeypatch.setenv("BANKQL_API_KEYS", "")
    (tmp_path / "api_keys.yaml").write_text(f"keys:\n  - id: ops\n    sha256: {hash_key('ops-key')}\n    run_sql: true\n")
    c = TestClient(create_app(make_agent(agent_fake({})), keys=KeyStore(tmp_path)))
    h = {"X-API-Key": "ops-key"}
    r = c.post("/v1/sql/run", json={"source": "bank_demo", "sql": "SELECT A3, COUNT(*) AS n FROM district GROUP BY A3 ORDER BY A3"}, headers=h)
    assert r.status_code == 200 and r.json()["row_count"] == 8 and r.json()["export_id"]
    for sql in ["DROP TABLE account", "SELECT full_name FROM client_profile", "SELECT * FROM trans"]:
        assert c.post("/v1/sql/run", json={"source": "bank_demo", "sql": sql}, headers=h).status_code == 400


def test_openapi_spec_lists_endpoints(client):
    paths = set(client.get("/openapi.json").json()["paths"])
    assert {"/v1/ask", "/v1/sources", "/v1/sources/{source}/tables", "/v1/sql/validate", "/v1/sql/run",
            "/v1/results/{export_id}.{fmt}", "/health", "/v1/chat/completions", "/v1/models"} <= paths


def test_demo_page_served_without_key_and_locked_down(client):
    r = client.get("/demo")
    assert r.status_code == 200 and "BankQL" in r.text and "/v1/ask" in r.text and "session_id" in r.text
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"] and r.headers["x-content-type-options"] == "nosniff"
    assert "innerHTML" not in r.text  # DB values are rendered with textContent only
