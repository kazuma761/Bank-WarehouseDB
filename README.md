# BankQL

Natural-language questions over bank data, answered by **local open-weight models** (llama.cpp) with
**read-only, validated SQL**. Lookups ("show client 12's accounts") use fixed parameterized SQL; analytics
("which districts have the most loans in debt?") go through a ported [Mercer](https://github.com/baselanaya/Mercer)
pipeline. Rows always come straight from the database. See `SPEC.md` for the full brief and decisions,
`PROGRESS.md` for status, `docs/THIRD_PARTY.md` for what was reused and how it differs from the brief.

```
question → SQLAS input guard → (follow-up rewrite) → router (Gemma, JSON schema)
   ├─ lookup    → deterministic resolver → fixed tools (get_client/accounts/statement/cards/loans/orders)
   └─ analytics → Mercer (Arctic-Text2SQL) → sqlglot validator (≤2 tries) → read-only run + CSV/Parquet export
→ plausibility checks → SQLAS output guard → rows + SQL + 1–2 line summary (numbers checked) → DuckDB audit log
```

## Quickstart (CPU, synthetic data)

```bash
uv sync                                   # Python 3.12 venv, exact pins from uv.lock
cp .env.example .env
sudo scripts/setup_postgres.sh            # local PG16: DB + bankql_admin + read-only bankql_ro (passwords → .env)
.venv/bin/python scripts/make_fixture.py  # synthetic BIRD-financial fixture → SQLite, DuckDB, Postgres
.venv/bin/python -m pytest -m "not real_llm"
```

Models (one at a time on CPU; both resident on a GPU — see "GPU" below):

```bash
uv venv -p 3.12 .venv-llama && CMAKE_ARGS="-DGGML_NATIVE=ON" uv pip install -p .venv-llama "llama-cpp-python[server]==0.3.16"
.venv-llama/bin/python -m llama_cpp.server --model models/<gemma-12b>.gguf --port 8080 --n_threads 4 --n_ctx 8192
.venv/bin/uvicorn bankql.api:app --port 8000
```

`config/models.yaml` holds endpoints, the Gemma GGUF repo/file (owner-supplied), the router confidence
threshold (0.6 is a **starting value**, to be calibrated on the Phase 3 lookup eval) and `sql.mode`
(`fast` = 1 candidate at T=0, no decomposition; `full` = Mercer's 6 stages).

## Safety model

| Layer | What it does |
|---|---|
| Connection | SQLite opened `mode=ro` + `query_only`; DuckDB `read_only`; Postgres role `bankql_ro` (SELECT-only, `default_transaction_read_only=on`) + `SET TRANSACTION READ ONLY` per query. Every query in a rolled-back transaction with a statement timeout and row cap. |
| Validator (`bankql/guardrails.py`) | sqlglot AST: exactly one `SELECT`/`WITH…SELECT`; no DDL/DML anywhere (CTEs, subqueries), no `SELECT INTO`, `FOR UPDATE`, PRAGMA/ATTACH/COPY/SET; no file/sleep/settings functions or table functions; allowlisted tables only. What runs is re-rendered from the validated AST. |
| PII | Output-column **lineage** (sqlglot qualify + scopes): PII may be filtered on or `COUNT`ed, never returned by analytics SQL (`CONCAT`, `CASE`, CTEs, subqueries, `UNION`, window functions all traced). Lookup tools may return the synthetic name and masked phone. PII values are never sent to the model as schema samples. |
| Large tables | Tables flagged in `sources.yaml` need a bounding filter on their `time_column` once the EXPLAIN row estimate exceeds the threshold. |
| SQLAS | `check_input` (injection / malicious intent / credential / bypass block; bulk-PII patterns are audited warnings), `check_sql` on comment-free SQL, `check_output` on PII cells + date-stripped summary. |
| Refusal | Low router confidence (intent logprobs when the server returns them), IDs/dates not written in the question, validation failing twice, empty results, row explosion, ungrounded summary numbers (summary replaced). |

## Data sources

`config/sources.yaml` names each source; the SQLAlchemy URL comes from an **environment variable** (`url_env`).
Each source has a dialect, table allowlist, semantic YAML, row cap, statement timeout and large-table flags.
SQLite, DuckDB and PostgreSQL are implemented and tested. Others need only their driver + config:

| Database | Driver (`uv pip install …`) | URL example (`url_env` value) | Read-only |
|---|---|---|---|
| MySQL / MariaDB | `pymysql` | `mysql+pymysql://ro@host/bank` | grant SELECT only; `SET SESSION TRANSACTION READ ONLY` |
| SQL Server | `pyodbc` | `mssql+pyodbc://ro@dsn` | `db_datareader` role |
| Oracle | `oracledb` | `oracle+oracledb://ro@host/?service_name=X` | SELECT grants / read-only user |
| Snowflake | `snowflake-sqlalchemy` | `snowflake://ro@acct/db/schema?warehouse=W` | role with USAGE + SELECT |
| BigQuery | `sqlalchemy-bigquery` | `bigquery://project/dataset` | `roles/bigquery.dataViewer` + jobUser |
| Trino | `trino[sqlalchemy]` | `trino://ro@host:8443/catalog` | Trino access control (`bankql/connectors/trino.py`) |

For those dialects the DB role is the driver-level read-only guarantee (the validator still applies), and
the large-table estimate falls back to `COUNT(*)` until that dialect's EXPLAIN parser is added.

### Federation

- **Dev:** `federations:` in `sources.yaml` builds an in-memory DuckDB that `ATTACH`es members read-only;
  queries use `member.table` names (`financial.trans JOIN bank_pg.account`). SQLite/Postgres/MySQL members
  need DuckDB's `sqlite_scanner` / `postgres_scanner` / `mysql_scanner` extensions (downloaded from
  `extensions.duckdb.org` on first use, or pre-installed with `INSTALL … FROM '<dir>'`). DuckDB-file members
  attach natively.
- **Production:** Trino. One source with `dialect: trino` and a `catalog.schema.table` allowlist; see the
  example at the bottom of `config/sources.yaml` and `bankql/connectors/trino.py`.

## Integrations

| Surface | Entry point | Example |
|---|---|---|
| Python | `from bankql import Agent; Agent.from_config("config/").ask(q, source=…, session_id=…)` | `examples/python_library.py` |
| REST | `uvicorn bankql.api:app` — `/v1/ask` (+SSE), `/v1/sources`, `/v1/sources/{s}/tables`, `/v1/sql/validate`, `/v1/sql/run`, `/v1/results/{id}.csv`, `/health`; OpenAPI at `/openapi.json` | `examples/rest_curl.sh` |
| MCP | `python -m bankql.mcp_server [--transport http]` — `ask`, `lookup`, `list_sources`, `list_tables`, `describe_table`, `validate_sql`, `run_sql` | `examples/mcp_claude_desktop.json`, `examples/mcp_http.md` |
| OpenAI-compatible | `POST /v1/chat/completions`, model `bankql` or `bankql:<source>` | `examples/openai_compatible.py`, `examples/open_webui.md` |

Auth: `X-API-Key: <key>` or `Authorization: Bearer <key>`; keys from `BANKQL_API_KEYS` (comma-separated,
constant-time comparison, fail closed when unset). Docker: `Dockerfile` + `docker-compose.yml`
(API + Redis + Postgres; llama-server on the host).

## GPU (RTX 4080 Super, 16 GB)

Phase 6 — `scripts/serve_local.sh|.ps1` and `scripts/eval_local.*` are still to be written (PROGRESS.md
"Later"). Target layout: Gemma on :8080 (`-ngl 99 -c 8192`), Arctic on :8081
(`-ngl 99 -c 24576 -np 3 --cache-type-k q8_0 --cache-type-v q8_0`; depending on the llama.cpp version the
quantized V cache may need flash attention, `-fa`).

## Layout

```
bankql/  agent.py router.py resolver.py tools/ analytics/ (+ mercer/ port) guardrails.py connectors/
         semantic/ llm/ export.py audit.py api/ mcp_server.py
config/  sources.yaml models.yaml semantic/financial.yaml
eval/    harness + metrics (resumable JSONL)        scripts/  fixture, Postgres setup, gates
tests/   pytest (FakeLLM; real DBs)                 third_party/  Mercer + BIRD mini_dev (pinned submodules)
```
