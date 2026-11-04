# PROGRESS

Running log. Acceptance gates live in `GATES.md` (checked with
`node <unlazy>/scripts/gate-check.mjs GATES.md`, or directly with
`.venv/bin/python scripts/gate.py <gate>`).

## Status

| Step (2026-11-04 plan) | State |
|---|---|
| 1. Skeleton, git, SPEC.md | done |
| 2. Mercer pinned + read; SQLAS read; differences noted | done — `docs/THIRD_PARTY.md` (8 SQLAS + 8 Mercer differences) |
| 3. Synthetic fixture → SQLite / DuckDB / Postgres | done — 9 tables, 5,916 rows, counts match in all three |
| 4. Phase 2 core | done, **except** SQLite⋈Postgres federation: blocked (DuckDB extensions, see Blockers) |
| 5. Semantic YAML + Mercer mappings adapter | done — accepted by ported SemanticMapper on all 3 DBs |
| 6. AnalyticsEngine + Mercer adapter (fast/full) on FakeLLM | done — our validator runs inside Mercer's executor; no torch/triton import |
| 7. Python lib, REST, MCP, OpenAI-compatible endpoint | done — e2e on FakeLLM; real servers start; **Docker image build untested** (Docker Hub 429) |
| 8. Resumable eval harness + metrics + SQLAS hooks | done — our BIRD EX = official `calculate_ex` on 9 edge cases |
| 9. llama-cpp-python[server] builds from PyPI | done — 0.3.16, built in 1m31s, in `.venv-llama` |

Test suite: **435 passed, 1 skipped** (`.venv/bin/python -m pytest`), against real SQLite, DuckDB and
PostgreSQL 16 with `FakeLLM`. The skip is the federation blocker below. No real-model test has run yet.

## Blockers needing an owner decision

1. **DuckDB federation of SQLite/Postgres members.** `ATTACH … (TYPE sqlite|postgres)` needs DuckDB's
   `sqlite_scanner` / `postgres_scanner` extensions, downloaded from `extensions.duckdb.org`, which this
   session's network policy denies. Code, config and validation are done (`bankql/connectors/federation.py`;
   cross-member validation is tested; DuckDB-file members attach natively and are tested). Options:
   (a) allow `extensions.duckdb.org`; (b) approve the third-party PyPI repackaging
   `duckdb-extension-sqlite-scanner` / `-postgres-scanner` (santosh-d3vpl3x — I have not adopted it:
   unreviewed native binaries); (c) approve a fallback that copies member tables into DuckDB.
   Until then `tests/test_federation.py::test_sqlite_joined_to_postgres` skips with the reason.
2. **Docker image build untested.** `docker build` failed with `429 Too Many Requests` from Docker Hub
   pulling `python:3.12-slim`. `Dockerfile`, `docker-compose.yml`, `scripts/pg_init.sh` are written but
   unverified.

## Log

- **Step 8** — `bankql/eval/`: resumable `run_jsonl` (append + fsync per item, torn-line tolerant,
  errors retried, `--limit` chunks), BIRD-faithful set EX, multiset EX, SQLAS `execution_accuracy` +
  `classify_failure` (fed the shapes its source reads), lookup metrics (intent / args / rows / refusal /
  latency), BIRD loader that searches the unzipped layout and drops gold SQL that fails, leakage guard over
  every prompt text. CLIs `eval/run_analytics.py`, `eval/run_lookup.py`. Added `bird_financial` source
  (original BIRD SQLite, no large-table guard for the benchmark).
- **Step 7** — `Agent` (SPEC §2 flow), REST (`X-API-Key` or Bearer, constant-time, fail-closed; SSE;
  guarded `/v1/sql/run`; exports), OpenAI-compatible `/v1/chat/completions` + `/v1/models` (stateless:
  follow-ups from the message history), MCP server on the **mcp 2.3.0** SDK — `FastMCP` is now
  `MCPServer`. Found and fixed: a dead llama-server made `/v1/ask` return HTTP 500; it now returns a clear
  `status: error`. Router hardening: IDs/years must appear in the question; intent-logprob confidence.
- **Step 6** — fast mode composes Mercer stages 1, 2, 4 (direct_cot @ T=0), 5, 6; full mode runs
  `MercerPipeline`. Mercer patch #4: candidates are validated *before* Mercer's EXPLAIN gate (it ran
  EXPLAIN on raw LLM SQL). PII values excluded from M-Schema samples. Dialect told via a backend wrapper.
- **Step 5** — found: Postgres folds `A2` → `a2`, so descriptions keyed `A2` silently vanished there
  (and in Mercer). Semantic lookups are now case-insensitive and mappings are generated per source.
- **Step 4** — validator, PII lineage (qualify + scopes; COUNT exempt), large-table rule (EXPLAIN
  estimates on Postgres/DuckDB, `MAX(rowid)` on SQLite), SQLAS policy wrapper, resolver, six lookup tools,
  CSV/Parquet exporter (formula-injection safe), DuckDB audit log, federation, LLM client + FakeLLM.
  Found and fixed: DuckDB `REAL` is 4-byte (BIRD SQLite's is 8-byte) → fixture uses `DOUBLE PRECISION`;
  `duckdb_engine` reflection breaks on DuckDB 2.0 → catalog via `information_schema`.
- **Step 3** — deterministic Faker fixture (seed 42) with BIRD's DDL, Czech codes, quoted `"order"`,
  planted name collisions (two "Rahul Sharma", "Priya Patel"/"Priya Patil"); Postgres roles via
  `scripts/setup_postgres.sh` (random passwords → `.env`, never printed).
- **Step 2** — pins and every difference from SPEC recorded in `docs/THIRD_PARTY.md`.
- **Step 1** — repo skeleton; deps via `uv` (`uv.lock` pins exact versions; `requirements.lock`
  exported); Mercer and BIRD `mini_dev` added as pinned git submodules.
- **Step 9** — `llama-cpp-python[server]==0.3.16` builds from source from PyPI
  (`CMAKE_ARGS=-DGGML_NATIVE=ON`) into `.venv-llama`, separate so its pins can't clash with ours.
- **Phase 0** — Python 3.13 system / 3.12 venv; 4 vCPU (AVX-512, AMX), 15 GB RAM, 31 GB disk.
  Redis 7 and PostgreSQL 16.15 start. PyPI + `git clone` from GitHub work. Blocked by network policy:
  `huggingface.co`, `bird-bench.oss-cn-beijing.aliyuncs.com`, GitHub release/API downloads,
  `extensions.duckdb.org`.

## Later (deliberately not attempted in this session)

Each item says exactly what unblocks it.

1. **BIRD mini-dev download (Phase 1 real data).**
   Unblock: add `bird-bench.oss-cn-beijing.aliyuncs.com` to the environment's allowed domains
   (environment settings → Network access). Then: download + unzip `minidev.zip` into `data/bird/`,
   inspect the real layout (`bankql.eval.bird.load_bird` searches for `mini_dev_sqlite.json` and
   `financial.sqlite`), set `BANKQL_BIRD_URL`, check every gold SQL executes, rebuild
   `data/bank_demo.duckdb` + Postgres from the real tables (keep the BIRD SQLite untouched), regenerate
   column descriptions from `database_description/*.csv` and diff them against
   `config/semantic/financial.yaml`.
2. **Model downloads (Arctic-Text2SQL-R1-7B i1-IQ4_XS, Gemma 12B).**
   Unblock: allow `huggingface.co`, `cdn-lfs.huggingface.co`, `cas-bridge.xethub.hf.co`; owner supplies the
   exact Gemma GGUF repo + filename (`config/models.yaml` → `router.hf_repo` / `router.hf_file`, or env
   `BANKQL_ROUTER_HF_REPO` / `BANKQL_ROUTER_HF_FILE`); if gated, add `HF_TOKEN` as an environment secret.
3. **Structured-output form + logprobs check.** Needs a loaded model: confirm whether the server accepts
   `response_format: {type: json_schema}` or llama-server's top-level `json_schema` field
   (`bankql/llm/__init__.py` supports both; `structured_output: auto` tries the first and falls back), and
   whether it returns `logprobs` for the intent tokens (otherwise self-reported confidence is used).
4. **Phase 3 — router + lookup eval (Gemma, CPU).** Needs 1–3. Generate the ~60-question lookup set from
   the real data (formal / casual / typos / Hinglish; name lookups via `client_profile`, ambiguous names,
   missing IDs; none may overlap prompt text — `bankql.eval.leakage` checks), run `eval/run_lookup.py` in
   chunks, calibrate `router.confidence_threshold` (starting value 0.6) from the results.
5. **Phase 4 — analytics eval (Arctic, CPU).** Needs 1–2. `eval/run_analytics.py --bird-dir data/bird
   --mode fast --evidence both` in chunks (`--limit`); full Mercer mode on a 10-question subset (`--ids`).
6. **Phase 5 real-model smoke tests.** One per surface (library, REST, MCP, OpenAI-compatible) with the
   real models, marked `real_llm`. Needs 2.
7. **Phase 6 — local GPU handoff.** Write `scripts/serve_local.sh|.ps1`, `scripts/eval_local.sh|.ps1`,
   README GPU quickstart; testing needs the owner's RTX 4080 Super.
8. **Per-user identity → database roles.** Pass the end user's identity through all four entry points
   (library, REST, MCP, OpenAI-compatible) and map it to database roles so row-level security applies per
   person.
9. **Federation + Docker** — see Blockers above.
