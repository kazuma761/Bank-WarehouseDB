# Latency (C)

Measured 2026-11-04 with `eval/latency.py --reps 3` (in-process, FakeLLM, `bank_demo` DuckDB fixture, 102 turns =
34 turns of `eval/chat_smoke.py` × 3). **LLM time is ~0 here by construction**: these numbers are BankQL's own
overhead (Python + database). Real-model time is reported per call in `AskResult.llm_calls` (prompt / completion /
cached tokens, prefill and decode ms from llama-server's `timings`) and will dominate — see "Not measured yet".

Raw data: `eval/results/latency_report.json`.

## Before vs after

| | original (start of C) | before (toggles off) | **after** |
|---|---|---|---|
| request mean | 40.9 ms | 27.9 ms | **11.2 ms** |
| request p50 / p95 | 17.1 / 77.3 ms | 8.2 / 52.9 ms | **6.2 / 39.8 ms** |
| first question (cold start) | 266 ms | 205 ms | **31 ms** |
| LLM calls per analytics question | 5.2 | 5.2 | **1.87** |
| prompt characters per analytics question | 25,646 | 25,646 | **4,135 (−84 %)** |
| LLM calls per lookup | 1.08 | 1.08 | 1.08 |
| verified-cache hits (of 30 analytics turns) | – | 0 | 20 |

"original" = the code before any C change. "before" = current code with the C toggles off
(`link_schema: always`, no verified cache, no warm-up) — it already includes the validation memo and the sklearn
fix, which are not toggles. "after" = defaults.

What each change did:

| Change | Effect (measured) |
|---|---|
| `link_schema: auto` — full schema for ≤ 12-table sources, constant prompt prefix | 2 fewer LLM calls per analytics question; prompt size −84 %; the prefix up to `--- QUESTION ---` is byte-identical across questions (test), so llama-server's KV cache can reuse it |
| `cache_prompt: true` on every llama-server request | no effect without a model; enables prefix reuse above |
| Skip rewrite when there is no history | rewrite ran for 9 of 102 turns (only follow-ups) |
| Template summaries for lookups | 0 summary LLM calls on lookups (1.08 calls/lookup = the router only, + 1 rewrite on follow-ups) |
| Arctic output cap (`sql.max_tokens: 768`) + `<think>` stripping | no effect without a model; bounds decode time, keeps reasoning out of the SQL |
| Verified-query cache | repeat analytics questions skip router + SQL generation (20/30 hits over 3 reps) |
| Warm-up (catalog, `as_of`, Mercer schema + BM25/LSH index) at API start | first question 205 → 31 ms |
| Validation memo per (SQL, mode) | validation mean 9.7 → 1.2 ms (one request validated the same SQL 4–5 times) |
| sklearn input-validation skip around Mercer's LSH (scoped, same results) | entity retrieval p50 5.7 → 4.1 ms, p95 22.8 → 15.2 ms (isolated benchmark) |

## Per stage (after)

| Stage | p50 ms | p95 ms | mean ms | n |
|---|---|---|---|---|
| input guardrail (SQLAS, advisory) | 0.1 | 0.1 | 0.08 | 102 |
| rewrite (follow-ups only) | 0.1 | 0.1 | 0.10 | 9 |
| router | 0.1 | 0.1 | 0.08 | 82 |
| resolver | 2.1 | 8.6 | 2.5 | 51 |
| Mercer entity retrieval | 9.0 | 40.5 | 16.6 | 19 |
| Mercer schema linking (skipped: full schema) | 0.0 | 0.0 | 0.04 | 19 |
| Mercer SQL generation | 1.2 | 2.2 | 1.4 | 19 |
| validation | 0.03 | 8.4 | 1.2 | 85 |
| EXPLAIN | 2.9 | 3.8 | 3.0 | 13 |
| DB execution | 5.0 | 8.0 | 4.8 | 79 |
| Mercer execution + scoring | 8.0 | 10.8 | 6.7 | 19 |
| export + preview | 6.2 | 9.5 | 6.9 | 33 |
| summary | 0.1 | 0.2 | 0.12 | 30 |
| output guardrail | 0.1 | 0.1 | 0.06 | 69 |
| **total** | **6.2** | **39.8** | **11.2** | 102 |

LLM rows are FakeLLM bookkeeping. Spans overlap (e.g. `db_execute` happens inside resolver, export and Mercer
execution), so they do not sum to the total.

## Where the time goes (after, no model)

Share of request time: **database 36.6 %, BankQL Python 63.4 %, LLM 0 % (fake)**.

cProfile self-time by package: other (builtins, copy, duckdb_engine glue) 39 %, sqlglot 22 %,
sklearn/numpy/rapidfuzz 13 %, bankql 9 %, DB drivers 9 %, SQLAlchemy 5 %, Mercer port 2 %.

## Native rewrite?

Not proposed. The rule was "only for a component taking more than 50 ms per request after optimisation"; the
largest is Mercer entity retrieval at **16.6 ms mean / 40.5 ms p95** (isolated: 4.1 / 15.2 ms), and the whole
non-LLM path is 11.2 ms mean. With a 7B/12B model on CPU each LLM call is seconds, so BankQL's own overhead is
well under 1 % of a request. The next Python-level step, if ever needed: batch Mercer's per-token LSH queries into
one sklearn call per question (a Mercer patch; ~9 calls → 1 per question).

## Not measured yet (needs the models)

Prompt/completion tokens, cached-token reuse, prefill vs decode ms and tokens/s per call — recorded automatically
in `AskResult.llm_calls` and `eval/chat_smoke.py --url …` output once llama-server runs. Run
`eval/chat_smoke.py --url http://127.0.0.1:8000 --key $KEY` with the real models to fill this table, and compare
`link_schema: always` vs `auto` for the KV-cache effect.
