# PROGRESS

Running log. Newest entries at the top of each section. Acceptance gates live in
`GATES.md` (checked with `node <unlazy>/scripts/gate-check.mjs GATES.md`, or
directly with `.venv/bin/python scripts/gate.py <gate>`).

## Status

| Step (2026-11-04 plan) | State |
|---|---|
| 1. Skeleton, git, SPEC.md | done |
| 2. Mercer pinned + read; SQLAS read; differences noted | in progress |
| 3. Synthetic fixture → SQLite / DuckDB / Postgres | todo |
| 4. Phase 2 core | todo |
| 5. Semantic YAML + Mercer mappings adapter | todo |
| 6. AnalyticsEngine + Mercer adapter (fast/full) on FakeLLM | todo |
| 7. Python lib, REST, MCP, OpenAI-compatible endpoint | todo |
| 8. Resumable eval harness + metrics + SQLAS hooks | todo |
| 9. llama-cpp-python[server] builds from PyPI | done — 0.3.16, 1m31s, in `.venv-llama` |

## Log

- **Phase 0** — Python 3.13 system / 3.12 venv; 4 vCPU (AVX-512, AMX), 15 GB RAM,
  31 GB disk. Redis 7 and PostgreSQL 16.15 start. PyPI + `git clone` from GitHub
  work. **Blocked by network policy:** `huggingface.co`,
  `bird-bench.oss-cn-beijing.aliyuncs.com`, GitHub release/API downloads,
  `extensions.duckdb.org`.
- **Step 9** — `llama-cpp-python[server]==0.3.16` builds from source from PyPI
  (`CMAKE_ARGS=-DGGML_NATIVE=ON`) into a separate venv `.venv-llama`, so its
  FastAPI/pydantic pins can't conflict with ours. Server module imports.
- **Step 1** — repo skeleton; deps via `uv` (`uv.lock` pins exact versions);
  Mercer and BIRD `mini_dev` added as pinned git submodules.

## Later (deliberately not attempted in this session)

Each item says exactly what unblocks it.

1. **BIRD mini-dev download (Phase 1 real data).**
   Unblock: add `bird-bench.oss-cn-beijing.aliyuncs.com` to the environment's
   allowed domains (environment settings → Network access). Then run
   `scripts/fetch_bird.sh` (to write), inspect the real folder layout, filter
   `db_id == "financial"`, verify every gold SQL executes, rebuild
   `data/bank_demo.duckdb` + Postgres from the real tables, regenerate column
   descriptions from `database_description/*.csv` and diff them against
   `config/semantic/financial.yaml`.
2. **Model downloads (Arctic-Text2SQL-R1-7B i1-IQ4_XS, Gemma 12B).**
   Unblock: allow `huggingface.co`, `cdn-lfs.huggingface.co`,
   `cas-bridge.xethub.hf.co`; owner supplies the exact Gemma GGUF repo + filename
   (fill `config/models.yaml` → `router.hf_repo` / `router.hf_file`); if gated, add
   `HF_TOKEN` as an environment secret.
3. **Structured-output form check.** Needs a loaded model: confirm whether the
   server accepts `response_format: {type: json_schema}` or the top-level
   `json_schema` field (`bankql/llm/client.py` supports both, `structured_output:
   auto` probes), and whether it returns `logprobs` for the intent tokens.
4. **Phase 3 — router + lookup eval (Gemma, CPU).** Needs items 1–3. Generate
   the ~60-question lookup set from the real data (formal / casual / typos /
   Hinglish; name lookups, ambiguous names, missing IDs), run
   `eval/run_lookup.py` in chunks, calibrate `router.confidence_threshold`
   (starts at 0.6) from the results.
5. **Phase 4 — analytics eval (Arctic, CPU).** Needs items 1–2. Run
   `eval/run_analytics.py` on the financial questions in fast mode with and
   without evidence; full Mercer mode on 10 questions if time allows.
6. **Phase 6 — local GPU handoff.** Write `scripts/serve_local.sh|.ps1`,
   `scripts/eval_local.sh|.ps1`, README GPU quickstart; test needs the owner's
   RTX 4080 Super.
7. **Per-user identity → database roles.** Pass the end user's identity through
   all four entry points (library, REST, MCP, OpenAI-compatible) and map it to
   database roles so row-level security applies per person.
