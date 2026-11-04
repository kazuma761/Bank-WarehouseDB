"""OpenAI-compatible /v1/chat/completions (model 'bankql') end to end on FakeLLM."""
import json

import pytest
from fastapi.testclient import TestClient

from bankql.api import create_app
from tests.fakes import agent_fake, route

KEY = "k"


@pytest.fixture
def client(make_agent, monkeypatch):
    monkeypatch.setenv("BANKQL_API_KEYS", KEY)
    agent = make_agent(agent_fake({"in debt": route("analytics"), "client 12": route(tools=[{"tool": "get_statement", "account_id": 12}])},
                                  rewrites={"and in 1997?": "How many loans in debt were granted in 1997?"},
                                  summary="Some loans are in debt."),
                       {"granted in 1997": "SELECT COUNT(*) AS n FROM loan WHERE status = 'D' AND date >= '1997-01-01' AND date <= '1997-12-31'",
                        "in debt": "SELECT COUNT(*) AS n FROM loan WHERE status = 'D'"})
    return TestClient(create_app(agent), headers={"Authorization": f"Bearer {KEY}"})


def chat(client, messages, **kw):
    return client.post("/v1/chat/completions", json={"model": "bankql", "messages": messages, **kw})


def test_models_lists_bankql(client):
    assert "bankql" in {m["id"] for m in client.get("/v1/models").json()["data"]}


def test_reply_has_sql_and_markdown_table(client):
    r = chat(client, [{"role": "user", "content": "How many loans are in debt?"}]).json()
    msg = r["choices"][0]["message"]["content"]
    assert r["object"] == "chat.completion" and r["model"] == "bankql"
    assert "| n |" in msg and "```sql" in msg and "FROM loan" in msg


def test_lookup_reply_has_table_and_code_legend(client):
    r = chat(client, [{"role": "user", "content": "statement for client 12 account"}]).json()
    msg = r["choices"][0]["message"]["content"]
    assert r["bankql"]["status"] == "ok" and r["bankql"]["route"] == "lookup"
    assert "| trans_id | date | type |" in msg and "`PRIJEM` = credit" in msg


def test_history_drives_follow_up(client):
    msgs = [{"role": "user", "content": "How many loans are in debt?"}, {"role": "assistant", "content": "Some loans are in debt."},
            {"role": "user", "content": "and in 1997?"}]
    r = chat(client, msgs).json()
    assert r["bankql"]["standalone_question"] == "How many loans in debt were granted in 1997?"
    assert "1997" in r["bankql"]["sql"]


def test_streaming_format(client):
    with client.stream("POST", "/v1/chat/completions", json={"model": "bankql", "stream": True,
                                                             "messages": [{"role": "user", "content": "How many loans are in debt?"}]}) as s:
        lines = [ln for ln in "".join(s.iter_text()).splitlines() if ln.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    first = json.loads(lines[0][6:])
    assert first["object"] == "chat.completion.chunk" and "```sql" in first["choices"][0]["delta"]["content"]


def test_source_selected_via_model_name(client):
    r = chat(client, [{"role": "user", "content": "How many loans are in debt?"}], model="bankql:bank_pg")
    assert r.json()["bankql"]["source"] == "bank_pg"


def test_unknown_model_and_bad_messages(client):
    assert client.post("/v1/chat/completions", json={"model": "gpt-4", "messages": [{"role": "user", "content": "x"}]}).status_code == 404
    assert chat(client, [{"role": "assistant", "content": "hi"}]).status_code == 400
