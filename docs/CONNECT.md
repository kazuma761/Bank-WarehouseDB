# Connecting BankQL

BankQL is middleware: your application calls its REST API; BankQL calls local models (llama-server) and your
databases (read-only). Three parts: **1** app → BankQL, **2** BankQL → databases, **3** deployment.

---

## Part 1 — Application → BankQL (REST)

| | |
|---|---|
| Base URL | `http://<bankql-host>:8000` (binds `127.0.0.1` by default — see Part 3) |
| Auth header | `X-API-Key: <key>` (or `Authorization: Bearer <key>`) on every `/v1/*` call |
| Health | `GET /health` → `{"status":"ok"}` — no key needed (reveals nothing else); use it for liveness probes |
| OpenAPI | `GET /openapi.json` (generate a client in any language), docs at `/docs` |

### `POST /v1/ask`

Request:

```json
{
  "question":   "show client 12's accounts",      // required, 1–2000 chars
  "source":     "bank_pg",                         // optional; default = first source in sources.yaml
  "session_id": "user-42-conv-7",                  // optional; same id = follow-ups ("only 1997") work
  "user_id":    "emp-1033",                        // optional; RESERVED for per-user permissions — audited, not enforced yet
  "evidence":   null,                              // optional domain hint (evaluation use)
  "stream":     false                              // true = server-sent events with stage progress, then the result
}
```

Response (`200` for every answered request — read `status`):

```json
{
  "request_id": "9f1c…",                 "session_id": "user-42-conv-7",   "source": "bank_pg",
  "question": "…", "standalone_question": "…",           // the rewritten follow-up actually answered
  "status": "ok",                        // ok | refused | needs_clarification | error
  "route":  "lookup",                    // lookup | analytics | refused | clarify | error
  "intent": ["get_accounts"],
  "sql": "SELECT a.account_id, …",       // the exact SQL that ran (lookups: ';'-joined if several tools)
  "columns": ["account_id", "disposition", "frequency", "opened", "district_id"],
  "rows": [[12, "OWNER", "POPLATEK MESICNE", "1995-03-24", 18]],   // preview ≤ 100 rows, straight from the DB
  "row_count": 1,                        // full result size
  "export_id": null,                     // set when the full result is larger than the preview
  "results": [ {"name": "get_accounts", "sql": "…", "columns": […], "rows": […], "row_count": 1,
                "export_id": null, "legend": {"frequency": {"POPLATEK MESICNE": "monthly issuance"}}, "notes": []} ],
  "summary": "1 account for client_id 12.",
  "confidence": 0.97,                    // router confidence (intent logprobs when the model server returns them)
  "choices": [],                         // needs_clarification: the candidates to pick from (e.g. two "Rahul Sharma")
  "refusal_reason": null,                // refused / error: a sentence you can show the user
  "timings_ms": {"input_guardrail": 0.1, "router": 812.4, "lookup": 9.7, "total": 831.0},
  "warnings": []
}
```

How to handle `status`:

| status | meaning | what the app should do |
|---|---|---|
| `ok` | answered | show `summary`, `rows`/`results`, optionally `sql` |
| `needs_clarification` | ambiguous name / several accounts | show `refusal_reason` + `choices`; ask again with the chosen id (same `session_id`) |
| `refused` | not sure / not allowed / not found | show `refusal_reason`; do not retry unchanged |
| `error` | model server down or internal error | retry later; details are in the audit log, not the response |

### Other endpoints

| Method + path | Purpose |
|---|---|
| `GET /v1/sources` | configured sources (`name`, `dialect`, `description`, `as_of`, `available`) |
| `GET /v1/sources/{source}/tables` | allowlisted tables with column descriptions, coded values, PII flags |
| `POST /v1/sql/validate` `{source, sql}` | validate without running → `{ok, sql, code, reason, tables, warnings}` |
| `POST /v1/sql/run` `{source, sql, max_rows}` | run one validated SELECT (preview + `export_id`) |
| `GET /v1/results/{export_id}.csv` (or `.parquet`) | download a full result |

### HTTP status codes

| Code | When |
|---|---|
| 200 | `/v1/ask` answered (any `status`), other calls succeeded |
| 400 | `/v1/sql/run` SQL rejected by the validator — body `{"detail": {"code": "…", "reason": "…"}}` |
| 401 | missing / invalid API key (also when no keys are configured: BankQL fails closed) |
| 403 | the key is not allowed this source, or this operation (e.g. `/v1/sql/run` disabled for the key) |
| 404 | unknown source, export id, or expired export |
| 408 | `/v1/sql/run` exceeded the source's statement timeout |
| 422 | malformed request body (FastAPI validation; lists the bad fields) |
| 429 | per-key rate limit exceeded — honour `Retry-After` |

Error bodies never contain stack traces or schema internals.

### Timeouts

- **Client timeout:** set it from measured latency, not guesses. Without models the pipeline costs ~14 ms p50 /
  ~106 ms p95 (`eval/chat_smoke.py`). With models, LLM time dominates: one router call per lookup; router + 3
  Mercer calls (fast mode) per analytics question. Start with 60 s on a GPU host and 15 min on CPU, then tighten
  from the real-model `eval/chat_smoke.py --url …` run.
- **Server side:** each source has `statement_timeout_s` (default 10 s); router calls time out after
  `router.timeout_s` (120 s), SQL-model calls after `sql.timeout_s` (600 s) — `config/models.yaml`.

### Exports

When `row_count` > preview size, `export_id` is set. Download with the **same API key** that created it:

```bash
curl -H "X-API-Key: $KEY" -o result.csv "$BANKQL/v1/results/$EXPORT_ID.csv"
```

Export ids are random 128-bit values, are bound to the key that created them, and expire (default 24 h).

### Examples

**curl**
```bash
curl -s -X POST "$BANKQL/v1/ask" -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -d '{"question": "how many loans are in debt?", "source": "bank_pg", "session_id": "conv-1"}'
```

**Python**
```python
import httpx

bq = httpx.Client(base_url="http://127.0.0.1:8000", headers={"X-API-Key": KEY}, timeout=60)
r = bq.post("/v1/ask", json={"question": "show client 12's accounts", "session_id": "conv-1"}).json()
if r["status"] == "ok":
    print(r["summary"], r["columns"], r["rows"])
elif r["status"] == "needs_clarification":
    print(r["refusal_reason"], r["choices"])
else:
    print("not answered:", r["refusal_reason"])
```

**JavaScript (Node 18+ / browser)**
```js
const r = await fetch(`${BANKQL}/v1/ask`, {
  method: "POST",
  headers: { "X-API-Key": KEY, "content-type": "application/json" },
  body: JSON.stringify({ question: "only 1997", session_id: "conv-1" }),
  signal: AbortSignal.timeout(60_000),
});
if (!r.ok) throw new Error(`BankQL HTTP ${r.status}`);
const ans = await r.json();   // ans.status, ans.summary, ans.rows, ans.sql
```

**Java (11+, java.net.http)**
```java
var client = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();
var body = "{\"question\":\"statement for account 3 in December 1998\",\"session_id\":\"conv-1\"}";
var req = HttpRequest.newBuilder(URI.create(BANKQL + "/v1/ask"))
    .header("X-API-Key", KEY).header("Content-Type", "application/json")
    .timeout(Duration.ofSeconds(60))
    .POST(HttpRequest.BodyPublishers.ofString(body)).build();
var resp = client.send(req, HttpResponse.BodyHandlers.ofString());
// resp.statusCode() == 200 → parse resp.body() (Jackson/Gson): status, summary, columns, rows, sql
```

### API keys and per-key policy

- Simplest: `BANKQL_API_KEYS=key1,key2` — every key gets the default policy.
- Per-key policy: `config/api_keys.yaml` lists keys by **SHA-256 hash** (never the key itself) with a
  `sources` allowlist, `run_sql` (off by default) and per-minute limits:

```bash
.venv/bin/bankql hash-key --generate           # new key + its hash; give the key to the app, the hash to the YAML
echo -n "$EXISTING_KEY" | .venv/bin/bankql hash-key
```

```yaml
# config/api_keys.yaml
defaults: {sources: ["*"], run_sql: false, pii_filter: false, asks_per_minute: 60, lookups_per_minute: 30}
keys:
  - id: core-banking-app          # appears in the audit log instead of the key
    sha256: 3f1c…
    sources: [core_pg]            # 403 for any other source
    lookups_per_minute: 120       # 429 + Retry-After beyond this
```

Filtering on a PII column (e.g. "how many clients are named …") is **denied by default** (`400` with code
`pii_filter_denied` on `/v1/sql/run`; a refusal on `/v1/ask`). A key with `pii_filter: true` may do it, and then
BankQL returns only `COUNT`s and non-PII group keys, with counts below 5 as the string `"<5"` — handle that in your
UI. Name *lookups* ("show Rahul Sharma's accounts") are unaffected: the resolver handles them, not SQL filters.

---

## Part 2 — BankQL → your databases

Every source in `config/sources.yaml` reads its SQLAlchemy URL from an **environment variable** (`url_env`) —
credentials never go in YAML. After adding one, run:

```bash
.venv/bin/bankql init-source <name>     # writes config/semantic/<name>.yaml with TODO descriptions
.venv/bin/bankql check-source <name>    # connectivity, read-only (inspected, never by writing), timeout, tables
```

### `sources.yaml` templates

```yaml
sources:
  core_pg:                                   # PostgreSQL  (driver installed: psycopg)
    url_env: CORE_PG_URL                     # postgresql+psycopg://bankql_ro:***@db.internal:5432/core
    dialect: postgresql
    tables: [account, client, disp, loan, trans]
    semantic: [semantic/core_pg.yaml]
    row_cap: 100000
    statement_timeout_s: 10
    large_tables: {trans: {time_column: date}}
    large_scan_threshold_rows: 500000
    as_of: today

  core_mysql:                                # MySQL / MariaDB  (uv pip install pymysql)
    url_env: CORE_MYSQL_URL                  # mysql+pymysql://bankql_ro:***@db.internal:3306/core
    dialect: mysql
    tables: [account, client, loan]
    semantic: [semantic/core_mysql.yaml]

  core_mssql:                                # SQL Server  (uv pip install pyodbc; ODBC Driver 18)
    url_env: CORE_MSSQL_URL                  # mssql+pyodbc://bankql_ro:***@db.internal/core?driver=ODBC+Driver+18+for+SQL+Server&ApplicationIntent=ReadOnly
    dialect: mssql
    tables: [account, client, loan]
    semantic: [semantic/core_mssql.yaml]

  core_oracle:                               # Oracle  (uv pip install oracledb)
    url_env: CORE_ORACLE_URL                 # oracle+oracledb://bankql_ro:***@db.internal:1521/?service_name=CORE
    dialect: oracle
    tables: [account, client, loan]
    semantic: [semantic/core_oracle.yaml]

  files_sqlite:                              # SQLite (always opened mode=ro + query_only)
    url_env: FILES_SQLITE_URL                # sqlite:////data/extracts/core.sqlite
    dialect: sqlite
    tables: [account, trans]
    semantic: [semantic/files_sqlite.yaml]

  analytics_duckdb:                          # DuckDB (always opened read_only)
    url_env: ANALYTICS_DUCKDB_URL            # duckdb:////data/warehouse.duckdb
    dialect: duckdb
    tables: [trans_daily]
    semantic: [semantic/analytics_duckdb.yaml]
```

`check-source` inspects read-only guarantees automatically for PostgreSQL, SQLite and DuckDB. For MySQL, SQL
Server and Oracle it prints a WARN: confirm the grants below with your DBA. The validator (single SELECT,
allowlist, PII and large-table rules) applies to every dialect regardless.

### Read-only database users

**PostgreSQL**
```sql
CREATE ROLE bankql_ro LOGIN PASSWORD '<random>';
GRANT CONNECT ON DATABASE core TO bankql_ro;
GRANT USAGE ON SCHEMA public TO bankql_ro;
GRANT SELECT ON account, client, disp, loan, trans TO bankql_ro;        -- only the allowlisted tables
ALTER ROLE bankql_ro SET default_transaction_read_only = on;            -- checked by check-source
ALTER ROLE bankql_ro SET statement_timeout = '10s';
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
```

**MySQL 8 / MariaDB**
```sql
CREATE USER 'bankql_ro'@'10.0.%' IDENTIFIED BY '<random>';
GRANT SELECT ON core.account TO 'bankql_ro'@'10.0.%';                  -- repeat per allowlisted table
GRANT SELECT ON core.client  TO 'bankql_ro'@'10.0.%';
-- No server-side per-user statement timeout in MySQL; BankQL does not yet set MAX_EXECUTION_TIME (Later).
```

**SQL Server**
```sql
CREATE LOGIN bankql_ro WITH PASSWORD = '<random>';
USE core;
CREATE USER bankql_ro FOR LOGIN bankql_ro;
GRANT SELECT ON dbo.account TO bankql_ro;                              -- per table (avoid db_datareader: it reads everything)
DENY INSERT, UPDATE, DELETE, ALTER, EXECUTE TO bankql_ro;
```

**Oracle**
```sql
CREATE USER bankql_ro IDENTIFIED BY "<random>";
GRANT CREATE SESSION TO bankql_ro;
GRANT READ ON core.account TO bankql_ro;                               -- READ, not SELECT: no SELECT … FOR UPDATE locks
GRANT READ ON core.client  TO bankql_ro;
```

**SQLite / DuckDB** — files: make them readable but not writable by the BankQL service user
(`chown root:bankql core.sqlite && chmod 0440 core.sqlite`); BankQL opens them read-only anyway. A DuckDB file
must not be open by a writer process at the same time.

### Large time-series tables

Mark fact tables that are too big to scan whole:

```yaml
    large_tables: {trans: {time_column: date}}      # time_column defaults to the semantic YAML's time_column
    large_scan_threshold_rows: 500000               # EXPLAIN estimate above which a date filter is required
    as_of: today                                    # "last 3 months" counts back from here ("data" = latest date in the table)
```

A query on `trans` without a bounding filter on `trans.date` is refused with a hint once the table's EXPLAIN
row estimate exceeds the threshold. Lookup statements default to the last 3 months.

---

## Part 3 — Deployment

| Component | Port | Binds | Auth |
|---|---|---|---|
| BankQL API (uvicorn) | 8000 | `127.0.0.1` (compose: `BANKQL_BIND`) — put your app or a reverse proxy in front | API key |
| llama-server, Gemma (router/summaries) | 8080 | `127.0.0.1` only | `--api-key $BANKQL_LLAMA_API_KEY` |
| llama-server, Arctic (SQL) | 8081 | `127.0.0.1` only | `--api-key $BANKQL_LLAMA_API_KEY` |
| Redis (Mercer schema cache) | 6379 | `127.0.0.1` only | `--requirepass $BANKQL_REDIS_PASSWORD` |
| PostgreSQL (if you use the bundled one) | 5432 | `127.0.0.1` | role `bankql_ro` |

**Where llama-server runs:** on the same host as the API (GPU box), bound to localhost. In Docker, the API
container reaches it at `host.docker.internal`. CPU-only hosts can run one model at a time
(`BANKQL_DEMO_MODE=cpu scripts/demo.sh`); a 16 GB GPU holds both (`scripts/demo.sh`).

### Environment variables

| Variable | Used for |
|---|---|
| `BANKQL_API_KEYS` | comma-separated API keys (default policy) |
| `BANKQL_CONFIG_DIR` | config directory (default `config`) |
| `BANKQL_ROUTER_URL`, `BANKQL_SQL_URL` | llama-server endpoints (default `http://localhost:8080/v1`, `:8081/v1`) |
| `BANKQL_LLAMA_API_KEY` | key both llama-servers require |
| `BANKQL_ROUTER_HF_REPO`, `BANKQL_ROUTER_HF_FILE` | override the Gemma GGUF named in `config/models.yaml` |
| `HF_TOKEN` | Hugging Face token if a model repo is gated (`scripts/fetch_models.sh`) |
| `BANKQL_REDIS_URL`, `BANKQL_REDIS_PASSWORD` | Redis for Mercer's schema cache (`redis://:<pw>@127.0.0.1:6379/0`) |
| *per source* `url_env` | e.g. `BANKQL_FINANCIAL_URL`, `BANKQL_DEMO_URL`, `BANKQL_PG_URL`, `BANKQL_BIRD_URL`, `BANKQL_TRINO_URL` |
| `BANKQL_PG_ADMIN_URL` | fixture loading only (`scripts/make_fixture.py`) |
| `BANKQL_BIRD_DEMO_URL`, `BANKQL_BIRD_PG_URL` | BIRD financial copies (DuckDB + Postgres, with synthetic `client_profile`), written by `scripts/load_bird.py` / `setup_postgres.sh` |
| `BANKQL_BIRD_PG_ADMIN_URL` | loading the BIRD copy into Postgres (`scripts/load_bird.py`) |
| `BANKQL_ROUTER_GGUF`, `BANKQL_SQL_GGUF` | model files for `scripts/serve_local.*` (set by `setup_local`; default `models/<hf_file>`) |
| `BANKQL_NGL`, `BANKQL_FA_FLAG` | `serve_local`: GPU layers (default 99) and the flash-attention flag (default `-fa on`; older llama.cpp: `-fa`) |
| `BANKQL_PG_ADMIN_PASSWORD`, `BANKQL_PG_RO_PASSWORD` | docker-compose Postgres init |
| `BANKQL_MCP_API_KEY` | MCP server only: the key whose policy applies; MCP `run_sql` exists only if that key has `run_sql: true` |
| `BANKQL_MERCER_LOG_LEVEL` | Mercer log level (default `WARNING`) |
| `BANKQL_MERCER_GPU_KERNELS` | `1` = allow Mercer's torch/triton kernels (GPU host only) |
| `BANKQL_DEMO_MODE`, `BANKQL_PORT`, `BANKQL_BIND`, `REGISTRY` | demo script / compose / Docker build |
