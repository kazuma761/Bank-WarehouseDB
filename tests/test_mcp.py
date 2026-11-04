"""MCP server end to end via the mcp 2.x in-process Client, on FakeLLM."""
import json

import pytest
from mcp import Client

from bankql.mcp_server import build_server
from tests.fakes import agent_fake, route


@pytest.fixture
def server(make_agent):
    return build_server(make_agent(agent_fake({"client 12": route(tools=[{"tool": "get_cards", "client_id": 12}]),
                                               "in debt": route("analytics")}, summary="Some loans are in debt."),
                                   {"in debt": "SELECT COUNT(*) AS n FROM loan WHERE status = 'D'"}))


def data(result):
    if result.structured_content is not None:
        sc = result.structured_content
        return sc.get("result", sc) if isinstance(sc, dict) else sc
    return json.loads(result.content[0].text)


async def test_tools_are_listed_and_run_sql_off_by_default(server):
    async with Client(server) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert names == {"ask", "lookup", "list_sources", "list_tables", "describe_table", "validate_sql"}


async def test_ask_lookup_and_analytics(server):
    async with Client(server) as c:
        lk = data(await c.call_tool("ask", {"question": "cards of client 12", "source": "bank_demo"}))
        an = data(await c.call_tool("ask", {"question": "How many loans are in debt?", "source": "bank_pg"}))
    assert lk["status"] == "ok" and lk["route"] == "lookup"
    assert an["status"] == "ok" and an["route"] == "analytics" and an["sql"].startswith("SELECT")


async def test_lookup_tool_without_router(server):
    async with Client(server) as c:
        r = data(await c.call_tool("lookup", {"tool": "get_orders", "account_id": 3, "source": "financial"}))
        bad = data(await c.call_tool("lookup", {"tool": "drop_tables"}))
    assert r["tool"] == "get_orders" and '"order"' in r["sql"] and "error" in bad


async def test_catalog_tools(server):
    async with Client(server) as c:
        srcs = data(await c.call_tool("list_sources", {}))
        tables = data(await c.call_tool("list_tables", {"source": "bank_pg"}))
        desc = data(await c.call_tool("describe_table", {"source": "bank_pg", "table": "trans"}))
    assert {s["name"] for s in srcs} >= {"financial", "bank_demo", "bank_pg"}
    assert "trans" in tables and desc["time_column"] == "date"


async def test_validate_and_guarded_run(make_agent):
    from bankql.auth import Policy, Principal

    server = build_server(make_agent(agent_fake({})), Principal("ops", Policy(run_sql=True)))
    async with Client(server) as c:
        v = data(await c.call_tool("validate_sql", {"source": "financial", "sql": "WITH x AS (DELETE FROM loan RETURNING *) SELECT * FROM x"}))
        ok = data(await c.call_tool("run_sql", {"source": "financial", "sql": "SELECT COUNT(*) AS n FROM loan"}))
        bad = data(await c.call_tool("run_sql", {"source": "financial", "sql": "SELECT phone_masked FROM client_profile"}))
    assert not v["ok"] and v["code"] == "forbidden_node"
    assert ok["rows"] == [[70]] and bad["code"] == "pii_output"
