"""MCP server (mcp Python SDK 2.x — `MCPServer`, the renamed FastMCP) so Claude Desktop, Cursor and other
MCP clients can use BankQL as a plugin.

    python -m bankql.mcp_server                          # stdio
    python -m bankql.mcp_server --transport http --port 8765   # streamable HTTP at /mcp
"""
from __future__ import annotations

import argparse
import os

import anyio
from mcp.server.mcpserver import MCPServer

from bankql.agent import Agent
from bankql.auth import LIBRARY, KeyStore, Principal
from bankql.connectors import QueryRejected, QueryTimeout, jsonable
from bankql.tools import TOOL_NAMES, ToolError


def build_server(agent: Agent, principal: Principal | None = None) -> MCPServer:
    """`run_sql` is registered only for a principal whose policy enables it (off by default); PII filters need the
    principal's `pii_filter` permission (default deny)."""
    pii_filter = bool(principal and principal.policy.pii_filter)
    mcp = MCPServer(name="bankql", instructions=(
        "Read-only questions over bank data. Use `ask` for natural-language questions; `lookup` for one client's "
        "or account's records; `run_sql` only for a single SELECT (it is validated and may be refused)."))

    @mcp.tool(description="Ask a natural-language question. Returns rows from the DB, the SQL used and a summary.")
    async def ask(question: str, source: str | None = None, session_id: str | None = None) -> dict:
        r = await anyio.to_thread.run_sync(lambda: agent.ask(question, source, session_id, surface="mcp",
                                                             principal=principal or LIBRARY))
        return r.model_dump()

    @mcp.tool(description=f"Run one fixed lookup tool ({', '.join(TOOL_NAMES)}) without the LLM router.")
    async def lookup(tool: str, source: str | None = None, client_id: int | None = None, account_id: int | None = None,
                     date_from: str | None = None, date_to: str | None = None) -> dict:
        if tool not in TOOL_NAMES:
            return {"error": f"unknown tool {tool!r}; choose one of {TOOL_NAMES}"}
        src = source or next(iter(agent.cfg.sources))

        def run():
            t = agent.tools(src)
            if tool == "get_statement":
                return t.get_statement(account_id, date_from, date_to)
            if tool == "get_orders":
                return t.get_orders(account_id)
            if tool == "get_loans":
                return t.get_loans(client_id=client_id, account_id=account_id)
            return getattr(t, tool)(client_id)
        try:
            tr = await anyio.to_thread.run_sync(run)
        except (ToolError, KeyError) as e:
            return {"error": str(e)}
        return {"tool": tr.tool, "sql": tr.sql, "params": tr.params, "columns": tr.columns,
                "rows": [[jsonable(v) for v in r] for r in tr.rows[:100]], "row_count": len(tr.rows),
                "legend": tr.legend, "notes": tr.notes}

    @mcp.tool(description="List configured data sources.")
    def list_sources() -> list[dict]:
        return [{"name": n, "dialect": s.dialect, "description": s.cfg.description} for n, s in agent.sources.items()]

    @mcp.tool(description="List the allowlisted tables of a source.")
    async def list_tables(source: str) -> list[str]:
        src = agent.source(source)
        return await anyio.to_thread.run_sync(lambda: [f"{q}.{m.name}" if q else m.name for (q, _), m in src.catalog.tables.items()])

    @mcp.tool(description="Describe one table: columns, types, descriptions, coded values, PII flags.")
    async def describe_table(source: str, table: str) -> dict:
        return await anyio.to_thread.run_sync(lambda: agent.source(source).describe_table(table))

    @mcp.tool(description="Validate SQL without running it (read-only, allowlist, PII and large-table rules).")
    async def validate_sql(source: str, sql: str) -> dict:
        v = await anyio.to_thread.run_sync(lambda: agent.source(source).validate(sql, allow_pii_filter=pii_filter))
        return {"ok": v.ok, "sql": v.sql, "code": v.code, "reason": v.reason, "warnings": v.warnings}

    if not (principal and principal.policy.run_sql):
        return mcp

    @mcp.tool(description="Run one validated SELECT (guarded: refused unless it passes validate_sql). Max 100 rows.")
    async def run_sql(source: str, sql: str, max_rows: int = 100) -> dict:
        src = agent.source(source)
        try:
            r = await anyio.to_thread.run_sync(lambda: src.run(sql, max_rows=min(max(1, max_rows), 100), allow_pii_filter=pii_filter))
        except QueryRejected as e:
            return {"error": e.validation.reason, "code": e.validation.code}
        except QueryTimeout as e:
            return {"error": str(e), "code": "timeout"}
        agent.audit.write(surface="mcp_sql", source=source, route="sql", sql=r.sql, row_count=len(r.rows),
                          latency_ms=r.elapsed_ms, refused=False)
        return {"sql": r.sql, "columns": r.columns, "rows": [[jsonable(v) for v in x] for x in r.rows], "truncated": r.truncated}

    return mcp


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--config", default=os.environ.get("BANKQL_CONFIG_DIR", "config"))
    a = ap.parse_args()
    agent = Agent.from_config(a.config)
    # run_sql is enabled only if BANKQL_MCP_API_KEY names a key whose policy has run_sql: true.
    key = os.environ.get("BANKQL_MCP_API_KEY")
    server = build_server(agent, KeyStore(a.config).authenticate(key) if key else None)
    if a.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=a.host, port=a.port)


if __name__ == "__main__":
    main()
