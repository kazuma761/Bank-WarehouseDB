# PROGRESS

Running log. Acceptance gates live in `GATES.md` (checked with
`node <unlazy>/scripts/gate-check.mjs GATES.md`, or directly with
`.venv/bin/python scripts/gate.py <gate>`).

## Status

### Round 4 (2026-11-05: plug in your own data)

| Item | State |
|---|---|
| `/admin` page + `/v1/admin/*` (import CSV / TSV / Parquet into DuckDB; connect a database by URL; remove) — admin keys only | done; tested (API + Playwright walkthrough of /admin and /demo with FakeLLM) |
| `bankql import / connect / remove-source` | done + test |
| Saved only when `check-source` passes (writable roles refused); semantic skeleton per table with PII flags; `TODO` placeholders ignored when loading | done + tests |
| Plugged-in sources skip the bank router (analytics only); Mercer schema cache keyed by live columns | done + tests |
| `/demo`: keeps the chosen source when the list reloads; `?source=` preselects | fixed (found in the browser walkthrough) |

### Round 3 (2026-11-04: local GPU handoff, PII-filter permission)

| Item | State |
|---|---|
| `scripts/setup_local.sh/.ps1` (BIRD download → SQLite untouched + DuckDB + Postgres `bankql_bird`; Arctic download; Gemma path; DuckDB extensions; check-source) | written; loader tested on a BIRD-shaped DB (original unchanged, counts match, broken gold caught); `.ps1` not runnable here (no PowerShell) |
| `scripts/serve_local.sh/.ps1` (both llama-servers, 127.0.0.1 + `--api-key`, Arctic `-np 3`, q8_0 K/V, `-fa`) | `.sh` tested against a stand-in llama-server (flags, auth, detach, stop); `.ps1` untested |
| `scripts/eval_local.sh/.ps1` (Phase 3 + Phase 4 fast/full × evidence + chat smoke + latency + REPORT.md) | `.sh` wiring run end to end with the stand-in (every step ran, errors recorded); real run on the owner's GPU |
| `eval/make_lookup_set.py` (60 items, 10 styles), `eval/latency_real.py`, `eval/make_report.py` | done + tests |
| README "Run locally on GPU" | done |
| PII-filter permission (`pii_filter`, default deny) | done — all paths, tests; SECURITY.md: with per-user identity, the intended fix for differencing |
| Docker | verify locally |

### Round 2 (2026-11-04: E, F, B, D, C + decisions)

| Item | State |
|---|---|
| Push to `kazuma761/Bank-WarehouseDB` branch `bankql` | done — pushed after GitHub access was fixed |
| E. `/demo` page + `scripts/demo.sh` / `.ps1` | done — browser-checked (3 cards, follow-up rewritten, no console errors) |
| F. `docs/CONNECT.md` + `bankql check-source` / `init-source` / `hash-key` | done — check-source PASS on 3 DBs, FAIL on the writable admin role (inspected, no writes) |
| B. `eval/chat_smoke.py` (31 conversations, 34 turns, `/v1/ask` only) | done on FakeLLM — 34/34 as expected; real-model run waits for models |
| D. Security hardening + `tests/security` (50 tests) + `docs/SECURITY.md` | done — all pass; residual risks listed (differencing attacks on counts first) |
| C. Latency: spans, LLM stats, optimisations, `eval/latency.py`, `docs/LATENCY.md` | done on FakeLLM — mean 40.9 → 11.2 ms, analytics LLM calls 5.2 → 1.87; no native rewrite warranted |
| SQLAS advisory only | done — enforcement = validator + PII lineage + small-count rule + our output check |
| Docker: ECR base image as build ARG | written; **verify locally** (owner decision: CloudFront layer host stays blocked here) |
| DuckDB pinned (`duckdb==2.0.0`) | done |
| Gemma GGUF | configured: `unsloth/gemma-3-12b-it-GGUF` / `gemma-3-12b-it-Q4_K_M.gguf` (owner's fallback) |
| A. Real BIRD data | **blocked** — `bird-bench.oss-cn-beijing.aliyuncs.com` still denied (re-checked 2026-11-04) |
| MCP / OpenAI-compatible | frozen as asked; tests green; MCP `run_sql` off by default |

Test suite: **543 passed, 1 skipped** (the skip = DuckDB-extension federation blocker).

### Round 1 (steps 1–9)

| Step (2026-11-04 plan) | State |
|---|---|
| 1. Skeleton, git, SPEC.md | done |
| 2. Mercer pinned + read; SQLAS read; differences noted | done — `docs/THIRD_PARTY.md` |
| 3. Synthetic fixture → SQLite / DuckDB / Postgres | done — 9 tables, 5,916 rows, counts match in all three |
| 4. Phase 2 core | done, **except** SQLite⋈Postgres federation: blocked (DuckDB extensions) |
| 5. Semantic YAML + Mercer mappings adapter | done |
| 6. AnalyticsEngine + Mercer adapter (fast/full) on FakeLLM | done |
| 7. Python lib, REST, MCP, OpenAI-compatible endpoint | done |
| 8. Resumable eval harness + metrics + SQLAS hooks | done |
| 9. llama-cpp-python[server] builds from PyPI | done — 0.3.16 in `.venv-llama` |

## Blockers needing the owner

1. **Network (session still denies):** `huggingface.co` (+ `cdn-lfs.huggingface.co`, `cas-bridge.xethub.hf.co`),
   `bird-bench.oss-cn-beijing.aliyuncs.com`, `extensions.duckdb.org`, and for Docker
   `d2glxqk2uabbnd.cloudfront.net` (ECR layer CDN). Allowed-domain changes apply to new sessions.

## Log

- **Final verification** — found an intermittent segfault (1 in 6 full runs): the SQLite engine built with
  `create_engine("sqlite://", creator=…)` got SQLAlchemy's `SingletonThreadPool`, whose cleanup closes
  connections other threads still use (Mercer full mode EXPLAINs 3 candidates in parallel; the API thread pool
  would hit it too). Fixed with an explicit `QueuePool` + a 24-thread regression test; 10 clean runs after.
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
10. **Offline / on-prem DuckDB extensions.** For installs without internet, ship the `sqlite_scanner` and
    `postgres_scanner` extension files built for the pinned DuckDB (2.0.0, `linux_amd64` / `windows_amd64`), e.g. in
    `vendor/duckdb_extensions/v2.0.0/<platform>/`, and load them with `SET extension_directory = …` or
    `INSTALL '<path>.duckdb_extension'` before `ATTACH`. Keep signature checking on; bump the files with every DuckDB pin.
11. **Real-model runs of B and C.** `eval/chat_smoke.py --url http://127.0.0.1:8000 --key $KEY` and the
    `AskResult.llm_calls` token / prefill / decode numbers; compare `sql.link_schema: always` vs `auto` for the
    KV-cache effect; tune `sql.max_tokens` (768) against truncated Arctic answers.
12. **Small-count rule follow-ups:** query budgets or DP noise for PII-filtered counts (differencing attacks);
    lineage-aware output check so CTE-wrapped counts are not falsely refused (docs/SECURITY.md).
13. **Scale-out:** move the rate limiter and sessions to Redis before running more than one API worker.
14. **Plugged-in data, next steps.** Excel files (save as CSV for now); several API workers would each need a
   restart to see sources added by another worker or by the CLI (one worker reloads on `/admin` changes); a
   router prompt for non-bank sources so they get "unsupported" and clarification handling instead of going
   straight to analytics; large-table guards for imported tables are set by hand in `sources.local.yaml`.
