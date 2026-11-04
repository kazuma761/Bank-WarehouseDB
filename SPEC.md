# BankQL — specification

A local-LLM natural-language → SQL agent for banking data. Runs entirely on
open-weight models served by llama.cpp (no cloud LLM API in the default code
path). Developed in a CPU-only cloud session (4 vCPU, 16 GB RAM, no GPU); must
also run on an RTX 4080 Super (16 GB VRAM) workstation with config changes only.

This file is the brief as given, plus the design decisions settled in review
(section 10). Where section 10 refines an earlier section, section 10 wins.

## 1. Hard rules

1. **Local models only.** All LLM calls go through an OpenAI-compatible HTTP
   endpoint (llama.cpp `llama-server`, or `llama-cpp-python`'s server as a
   fallback). No OpenAI/Anthropic/Gemini API calls in the default path.
2. **Read-only data access.** Only single `SELECT` / `WITH … SELECT` statements
   ever reach a database. Validate with the sqlglot AST (not keyword grep).
   Open SQLite with `?mode=ro`, use read-only roles/transactions elsewhere, set
   statement timeouts and row caps.
3. **Public/synthetic data only** in the cloud session: the BIRD "financial"
   database and generated synthetic data. Never real customer data.
4. **Don't guess third-party APIs.** Read Mercer's and SQLAS's source in the
   installed version before coding against them. Pin exact versions/commits.
5. **Honest evaluation.** Never put an evaluation question or its gold SQL into
   prompts, few-shot examples, the verified-query library, or the glossary.
   Report accuracy both with and without BIRD "evidence" hints, labelled.
6. **CPU reality.** At most one model loaded at a time in the cloud. Every
   evaluation script is resumable (append to JSONL, skip done items).

## 2. Flow

```
question
  → input guardrail (SQLAS GuardrailPipeline.check_input — no LLM)
  → router (Gemma 12B, JSON-schema-constrained output: intent + entities)
      ├─ LOOKUP intents → entity resolver (plain code) → fixed, parameterized SQL tools
      └─ ANALYTICS intents → analytics engine (Mercer pipeline on Arctic-Text2SQL-R1-7B)
  → SQL guardrail (our sqlglot validator + SQLAS check_sql) → read-only execution
  → output guardrail (SQLAS check_output with PII columns)
  → answer: rows from the DB (preview ≤100 rows + full-result export), the SQL, a 1–2 line summary
```

- Lookups never use generated SQL; the router extracts intent + arguments, the
  SQL lives in our tool functions with bound parameters.
- The LLM never retypes numbers or IDs. Results render from the result set. The
  summary model sees only small aggregates / a few rows; every number it states
  must appear in the data.
- Entity resolution is deterministic: IDs by exact match; names by fuzzy match
  against the full table. Multiple matches → ask the user to choose. None → say so.
- Refuse when unsure: low router confidence, validation failing twice, or
  implausible results (empty, row explosion) → "I'm not sure" with the reason.
- Follow-ups: short session history; rewrite a follow-up into a standalone
  question before routing; never mutate previous SQL in place.

## 3. Reuse

- **Mercer** (https://github.com/baselanaya/Mercer, MIT) as the analytics
  engine, pinned in `third_party/mercer`. Reuse its 6-stage pipeline, sqlglot
  read-only sandbox + EXPLAIN pre-flight, M-Schema prompts with inline sample
  values, `config/mappings.yaml` format, llama.cpp backend. Needs Redis. Wrap
  behind our own `AnalyticsEngine`; port modules into `bankql/analytics/` with
  attribution if it can't be imported as a library. Add a **fast mode**: one
  candidate at temperature 0, no decomposition stage.
- **SQLAS** (`pip install sqlas`, MIT): `GuardrailPipeline` at runtime; its
  evaluation functions, failure classification and BIRD helpers for testing.
  `llm_judge` = our local Gemma endpoint; prefer metrics that need no LLM.
- **talkdb** — ideas only: semantic YAML with `valid_values` and `aliases`,
  confidence-based refusal, pandas computes / LLM narrates, follow-up rewriting,
  correction / verified-query loop.

## 4. Data

- Primary: BIRD mini-dev "financial" (Berka / PKDD'99): `minidev.zip` from
  `https://bird-bench.oss-cn-beijing.aliyuncs.com/minidev.zip`; filter
  `db_id == "financial"` (~32 questions) as the analytics eval set; verify every
  gold SQL executes.
- Gotchas: `district.A2…A16` (describe from BIRD CSVs); Czech code values
  (`PRIJEM` = credit, `VYDAJ` = withdrawal, …) in the glossary; reserved table
  name `order` quoted per dialect; clients have no names — "customer" means
  `client_id`, linked to accounts via `disp` (OWNER / DISPONENT).
- `data/bank_demo.duckdb`: copy of financial + synthetic
  `client_profile(client_id, full_name, city, phone_masked)` (Faker `en_IN`).
  Original BIRD SQLite stays untouched for evaluation.
- Same tables in PostgreSQL 16 with a read-only role.
- Semantic model `config/semantic/financial.yaml` (single source of truth):
  table/column descriptions, `valid_values`, `aliases`, glossary (incl. Czech
  codes), join paths, metrics, `time_column` on fact tables. An adapter
  generates Mercer's `mappings.yaml` from it.

## 5. Models and serving

- SQL: `Arctic-Text2SQL-R1-7B` i1-IQ4_XS GGUF (`mradermacher/Arctic-Text2SQL-R1-7B-i1-GGUF`).
- Router / summary: Gemma 12B — exact GGUF to be supplied by the owner.
  Configurable via `config/models.yaml` + env vars.
- Cloud CPU: prebuilt llama.cpp binary, else `pip install "llama-cpp-python[server]"`;
  one model at a time, threads = 4, context 8192.
- Router uses OpenAI-style `response_format` / `json_schema` constrained
  decoding; detect which form the server accepts. Don't rely on Gemma tool-call
  parsing in `--jinja` mode.
- Local GPU: `scripts/serve_local.sh` / `.ps1` — Gemma on :8080 (`-ngl 99 -c 8192`),
  Arctic on :8081 (`-ngl 99 -c 24576 -np 3 --cache-type-k q8_0 --cache-type-v q8_0`;
  `-fa` may be needed for the quantized V cache).

## 6. Connecting to databases

- `config/sources.yaml`: named sources, each with a SQLAlchemy URL read from an
  env var, dialect, read-only flag, table/schema allowlist, per-source semantic
  YAML, row cap, statement timeout, "large table" flags with `time_column`.
  SQLite, DuckDB, PostgreSQL implemented and tested; MySQL, SQL Server, Oracle,
  Snowflake, BigQuery, Trino need only their driver + config (documented).
- Dialects: the SQL writer is told the dialect; sqlglot transpiles/validates.
- Federation: dev = DuckDB engine `ATTACH`ing SQLite/Postgres/MySQL so one query
  joins `source.table` across DBs (test: SQLite financial ⋈ Postgres copy).
  Production = Trino (adapter + config example).
- Large tables: if EXPLAIN estimates exceed a threshold, require a filter on
  `time_column`, else refuse helpfully. Lookup tools default to a sensible
  window (e.g. last 3 months of transactions).
- Large results: aggregate in SQL, preview 100 rows, stream full result to a
  CSV/Parquet export downloadable from the API.

## 7. Integration surfaces

1. Python: `from bankql import Agent; Agent.from_config("config/").ask(question, source="financial", session_id=...)`
   → typed result: route, SQL, preview rows, row count, export id, summary,
   confidence, timings, warnings.
2. REST (FastAPI): `POST /v1/ask`, `GET /v1/sources`, `GET /v1/sources/{s}/tables`,
   `POST /v1/sql/validate`, `POST /v1/sql/run` (guarded), `GET /v1/results/{id}.csv`,
   `GET /health`; optional SSE stage progress; API-key header auth; OpenAPI spec.
3. MCP server (FastMCP): tools `ask`, `lookup`, `list_sources`, `list_tables`,
   `describe_table`, `validate_sql`, `run_sql` (guarded); stdio + HTTP.
4. OpenAI-compatible `POST /v1/chat/completions` with model `bankql`; reply has
   the SQL and a markdown results table.

Plus `Dockerfile`, `docker-compose.yml` (API + Redis + Postgres; llama-server on
host), and `examples/` for each integration.

## 8. Phases

- **0** Environment check.
- **1** Data: BIRD → SQLite (original), DuckDB (+ `client_profile`), Postgres;
  semantic YAML + Mercer adapter. Done when all gold SQL executes and row counts
  match across all three.
- **2** Core (no LLM): connectors, validator/guardrails, entity resolver, lookup
  tools (`get_client`, `get_accounts`, `get_statement(account_id, from, to)`,
  `get_cards`, `get_loans`, `get_orders`), exporter, federation; unit tests with
  `FakeLLM`. Done when pytest is green and the validator rejects DDL/DML
  (incl. inside CTEs/subqueries), multi-statement input, and unfiltered scans of
  large tables.
- **3** Router + lookup eval (Gemma, CPU): ~60 questions generated from the data
  (formal, casual, typos, some Hinglish; name lookups, ambiguous names, missing
  IDs). Intent accuracy, argument exact-match, end-to-end row match, refusal
  correctness, latency.
- **4** Analytics eval (Arctic, CPU): BIRD financial in fast mode, with and
  without evidence; full Mercer mode on a 10-question subset if time allows.
  Execution accuracy, SQLAS failure classification, per-stage latency.
- **5** Integration surfaces with FakeLLM end-to-end tests + one real-model
  smoke test each.
- **6** Local GPU handoff: `scripts/serve_local.*`, `scripts/eval_local.*`,
  README quickstart (Windows + Linux).
- **7** `REPORT.md`: architecture (Mermaid), accuracy tables with sample sizes,
  CPU latency + expected GPU speed-up, failure patterns, limitations, next
  steps; ≥10 hand spot-checks recorded.

## 9. Repo layout

```
bankql/
  agent.py  router.py  resolver.py  tools/  analytics/  guardrails.py
  connectors/  semantic/  llm/  export.py  api/  mcp_server.py
config/  data/  eval/  scripts/  examples/  tests/  third_party/
```

`uv` venv, pinned dependencies, configurable timeouts. Every query is logged
(question, route, SQL, row count, latency, refusal reason) to a local DuckDB
audit table.

## 10. Decisions settled in review (2026-11-04)

- **Q1 Mercer integration:** port into `bankql/analytics/mercer/` changing only
  import paths (plus listed patches), MIT notice kept; pristine clone stays in
  `third_party/mercer` as a submodule. Drop `ModelRouter` and the cloud
  `api_backend`. Nothing may import `triton` (or `torch`) on CPU — use Mercer's
  CPU fallbacks.
- **Q2 SQLAS input check:** block only on prompt injection, malicious SQL
  intent, credential extraction, bypass attempts. The two bulk-PII patterns
  (`bulk_pii_request`, `pii_bulk_request`) are warnings, written to the audit log.
- **Q3 SQLAS output check:** `check_output` has no `pii_columns` parameter; pass
  it only cells from PII-flagged columns plus the summary text. Strip ISO dates
  (`YYYY-MM-DD`) from the summary before the phone regex sees it.
- **Q4 PII policy:** lookup tools may return name + masked phone. Analytics SQL
  may not output PII — enforced on **column lineage** (sqlglot qualify/lineage),
  so `CONCAT(full_name, …)`, `CASE`, and GROUP BY + select of a PII column are
  caught. Filtering on PII and `COUNT` / `COUNT(DISTINCT)` over PII stay allowed.
- **Q5 Metrics:** headline = BIRD's official execution accuracy (result rows
  compared as **sets**, as in `bird-bench/mini_dev` `evaluation_ex.py`), tested
  against their code. Secondary: multiset-strict EX and the SQLAS score.
- **Q6 Time windows:** relative periods count back from the source's `as_of`:
  `data` (latest value of the time column) for BIRD/fixtures; production
  default is today.
- **Router confidence:** 0.6 is a starting value only; configurable; calibrate
  on the Phase 3 lookup eval. Use intent logprobs from llama-server when
  available instead of self-reported confidence alone.
- **API keys:** `BANKQL_API_KEYS` env var, constant-time comparison.
- **Postgres role:** `bankql_ro`, SELECT-only, `default_transaction_read_only = on`,
  statement timeout.
- **Python 3.12 + uv.**
- **Later:** pass end-user identity through all four entry points and map it to
  database roles so row-level security applies per person.
