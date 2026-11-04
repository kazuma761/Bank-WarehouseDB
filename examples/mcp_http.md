# MCP over HTTP (Cursor, other agents)

    .venv/bin/python -m bankql.mcp_server --transport http --port 8765   # streamable HTTP at http://127.0.0.1:8765/mcp

Tools: `ask`, `lookup`, `list_sources`, `list_tables`, `describe_table`, `validate_sql`, `run_sql` (guarded).
For stdio clients (Claude Desktop) see `mcp_claude_desktop.json`. The HTTP transport has no auth of its own:
bind it to localhost or put it behind an authenticating proxy.
