# Third-party code: pins, what we reuse, and where it differs from SPEC.md

Read from the installed/pinned source on 2026-11-04, not from memory.

## Mercer — MIT — https://github.com/baselanaya/Mercer

- mercer commit: `d6f922f92c836aee68f56e2a67cb98813a205dd4` (2026-05-08, "Fix CI coverage failure…")
- Pristine clone: git submodule `third_party/mercer`. Ported copy: `bankql/analytics/mercer/`
  (see the header of `bankql/analytics/mercer/__init__.py` for the MIT notice and patch list).

Reused: 6-stage pipeline (entity retrieval BM25+LSH → CHESS schema linking →
decomposition → 3-strategy candidate generation → EXPLAIN-gated execution +
consistency selection → taxonomy correction), `ReadOnlySandbox`, `db/explain.py`,
M-Schema renderer (`prompts/m_schema.py`), `mappings.yaml` format
(`tables.<t>.description`, `tables.<t>.columns.<c>`, `glossary.<term>`),
`LlamaCppBackend`, Redis `SchemaCache`.

Differences from what SPEC.md assumed:

1. **Not importable as a library.** Top-level packages are named `core`, `db`,
   `config`, `schema`, `prompts`, `inference`, `kernels`; `config` collides with
   our `config/` dir; `core/logging.py` imports a global pydantic-settings
   singleton; `inference/router.py` imports `anthropic` / `openai`. → Ported
   (decision Q1). `inference/router.py`, `inference/api_backend.py`, `app/`,
   `eval/`, `scripts/` are not ported, so no cloud SDK is reachable.
2. **Introspection needs an async engine** (`SchemaIntrospector.introspect`
   does `async with engine.connect()` + `run_sync`); sandbox/explain/executor
   accept sync engines. DuckDB has no async SQLAlchemy driver. → Our adapter
   calls the introspector's sync helpers (`_extract_tables`,
   `_sample_values_for_tables`) on a sync connection, filtered to the source
   allowlist, and the pipeline is patched to accept a prebuilt schema.
3. **Sandbox coverage.** Blocks Insert/Update/Delete/Drop/Create/Alter/
   AlterColumn/TruncateTable/Merge anywhere in the AST and multi-statements.
   It does **not** block `exp.Command` (PRAGMA, ATTACH, SET, COPY…),
   `SELECT … INTO`, locking clauses, or file/sleep table functions
   (`read_csv`, `pg_sleep`…), has no table allowlist, fetches max 100 rows
   (3 sample rows kept), and sets a timeout only on Postgres. → Our validator
   (`bankql/guardrails.py`) is a superset; the pipeline is patched to take an
   injected sandbox that runs our validator before Mercer's own checks.
4. **llama.cpp backend** sends `response_format: {"type": "json_object"}` only —
   no JSON-schema constrained decoding, no logprobs. → The router uses our own
   client (`bankql/llm/client.py`); SQL generation keeps Mercer's backend.
5. **Prompts never state the SQL dialect.** → A backend wrapper appends the
   target dialect + quoting rules to every system prompt (Mercer prompt files
   untouched); output is re-validated/transpiled with sqlglot.
6. **GPU kernels:** `kernels/__init__.py` imports `torch` at import time and the
   kernel modules `try: import triton`. → Patched: torch/triton are imported only
   when `BANKQL_MERCER_GPU_KERNELS=1`; CPU fallbacks otherwise (decision Q1).
7. **Audit:** Mercer writes its own `AuditEntry` to `logs/audit.duckdb`. → We pass
   a no-op store and keep one audit table (`bankql/audit.py`) for all routes.
8. Mercer's README renames the GGUF to `Arctic-Text2SQL-R1-7B-IQ4_XS.gguf`
   (drops `.i1`). We keep the downloaded filename from SPEC.md.

## SQLAS — MIT — PyPI `sqlas==2.8.0` (https://github.com/thepradip/SQLAS)

Reused: `GuardrailPipeline.check_input/check_sql/check_output` at runtime;
`execution_accuracy`, `classify_failure` / `FailureCategory` in evaluation.

Differences from SPEC.md:

1. `check_output(response, result_data, question)` has **no `pii_columns`
   parameter**; it regex-scans the response text and the first 20 rows of
   `result_data["rows"]`. Its phone regex `\+?\d[\d\-\s().]{7,}\d` matches ISO
   dates (`1995-03-24`). → We pass only PII-flagged cells and strip ISO dates
   from the summary first (decision Q3).
2. `check_input` patterns `bulk_pii_request` / `pii_bulk_request` fire on
   ordinary banking questions ("show me all accounts of customer 1234", any
   "credit card"). → Downgraded to audited warnings (decision Q2).
3. `check_sql` flags `--`, `/*`, `UNION SELECT`, `EXCEPT SELECT`,
   `INTERSECT SELECT` as injection, and `read_only_compliance` returns 0 unless
   the text *starts* with `SELECT`/`WITH` (a leading comment or parenthesis
   fails). → We feed it the comment-free SQL regenerated from our AST, and
   keep SQLAS's own block decision on that.
4. `GuardrailPipeline(pii_columns=[])` silently falls back to the defaults
   (falsy check), so an empty list cannot disable it.
5. `execution_accuracy` is a weighted score: 60 % numeric output match
   (±0.5 tolerance, string values ignored), 20 % row-count match, 20 % speed
   relative to gold. It is **not** BIRD EX. → Headline metric is BIRD's own set
   comparison; SQLAS score reported as secondary (decision Q5).
6. `run_bird_benchmark` expects the full BIRD layout (`bird/dev/dev.json`,
   `dev/dev_databases/`) and an `llm_judge`; mini-dev ships
   `mini_dev_sqlite.json`. → We use SQLAS metric functions inside our own
   resumable runner rather than its benchmark runner.
7. `read_only_compliance` accepts only `Select`/`With` roots, so every `UNION` / `EXCEPT` / `INTERSECT`
   scores "not read-only" and `check_sql` blocks it. → We run `check_sql` on each SELECT branch.
   `schema_compliance` also reports CTE names as unknown tables. → schema issues are warnings only.
8. `check_output` does not block a single PII hit in result rows (score 1 − 0.1 = 0.9 ≥ its 0.85
   threshold, though it fills `block_reason`). → Since we only pass PII-column cells and date-free text,
   we block on any output issue.

### SQLAS role in BankQL (decision, 2026-11-04)

Advisory only: `check_input` / `check_sql` / `check_output` produce warnings (`sqlas_*` in `warnings`, and
`sqlas_input_would_block` when SQLAS itself would have blocked); enforcement is our validator, PII lineage,
small-count rule and output check. SQLAS evaluation functions and `classify_failure` are used in `bankql/eval`.

### SQLAS findings worth reporting upstream (sqlas 2.8.0)

1. `read_only_compliance` returns 0.0 for any root that is not `Select`/`With`, so every `UNION` / `EXCEPT` /
   `INTERSECT` is classified as a write and `check_sql` blocks it. Suggest: accept `exp.SetOperation` (and
   `exp.Subquery`) roots.
2. Its phone regex `\+?\d[\d\-\s().]{7,}\d` matches ISO dates (`1995-03-24`), so `check_output` flags any result
   or summary with a date. Suggest: exclude `\d{4}-\d{2}-\d{2}` or require ≥ 10 digits.
3. `check_output` scores one PII hit in result rows as 1 − 0.1 = 0.9 ≥ the default 0.85 threshold and does not
   block, yet fills `block_reason` — inconsistent result object.
4. `GuardrailPipeline(pii_columns=[])` silently uses the defaults (`pii_columns or DEFAULT_PII_COLUMNS`); there is
   no way to disable column-name PII matching.
5. `check_input` patterns `bulk_pii_request` (`show me all … customer`) and `pii_bulk_request` (any "credit card")
   block ordinary banking questions; `raw_sql_injection_attempt` blocks "show me the raw sql".
6. `schema_compliance` reports CTE names as unknown tables (`invalid_tables: ['x']` for `WITH x AS (…)`).
7. `check_sql` counts `--` / `/* */` comments and `UNION SELECT` as injection signals in model-generated SQL,
   where they are benign; documenting that these heuristics target user-concatenated SQL would help.
8. `run_bird_benchmark` assumes the full BIRD layout (`dev/dev.json`); mini-dev ships `mini_dev_sqlite.json`.

## BIRD mini_dev — https://github.com/bird-bench/mini_dev

- bird_mini_dev commit: `1c49a375e76c471e218564da3f4617513d33e371` (2026-10-28)
- `evaluation/evaluation_ex.py::calculate_ex` = `1 if set(predicted_res) == set(ground_truth_res) else 0`.
  `evaluation_utils.execute_sql` runs predicted then gold SQL on
  `sqlite3.connect(db_path)` with `fetchall()`; any exception or timeout
  (default 30 s) scores 0. `bankql/eval/metrics.py::bird_ex` reproduces this
  and `tests/test_eval.py` checks it against the imported official function.
- The repo does **not** include the databases or `mini_dev_sqlite.json`; those
  come from `minidev.zip` (blocked host — see PROGRESS.md "Later").
