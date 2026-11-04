"""Regenerate REPORT.md (Phase 7) from eval/results/. Sections with no results say "not run".

    .venv/bin/python eval/make_report.py
"""
from __future__ import annotations

import json
import random
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bankql.eval import latest, load_jsonl  # noqa: E402

RES = ROOT / "eval/results"
ARCH = """```mermaid
flowchart LR
  App[Your application] -->|REST /v1/ask + API key| API[BankQL API]
  API --> IG[Input check<br/>SQLAS advisory]
  IG --> RW{history?}
  RW -->|yes| REW[Follow-up rewrite<br/>Gemma]
  RW -->|no| VC
  REW --> VC{verified-query<br/>cache hit?}
  VC -->|hit| EXE
  VC -->|miss| R[Router<br/>Gemma, JSON schema]
  R -->|lookup| RES[Resolver<br/>exact ids, fuzzy names] --> T[Fixed SQL tools]
  R -->|analytics| M[Mercer fast/full<br/>Arctic-Text2SQL] --> V[Validator<br/>read-only, allowlist,<br/>PII lineage, small counts,<br/>large tables]
  T --> V
  V --> EXE[Read-only execution<br/>timeout, row cap, export]
  EXE --> P[Plausibility checks] --> OG[Output PII check] --> A[Rows + SQL + summary]
  EXE --> DB[(SQLite / DuckDB / Postgres)]
  API --> AUD[(Audit log<br/>PII masked)]
```"""


def _pct(x) -> str:
    return "–" if x is None else f"{100 * x:.1f} %"


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def lookup_section() -> str:
    recs = latest(load_jsonl(RES / "lookup.jsonl"))
    if not recs:
        return "_Not run yet (`scripts/eval_local.sh`)._"
    rows = ["| metric | value | n |", "|---|---|---|"]
    for f, label in (("intent_ok", "intent accuracy"), ("args_ok", "argument exact-match"), ("rows_ok", "end-to-end row match"),
                     ("status_ok", "refusal / clarification correctness")):
        vals = [r[f] for r in recs if r.get(f) is not None]
        rows.append(f"| {label} | {_pct(_mean([float(v) for v in vals]))} | {len(vals)} |")
    lat = sorted(r["latency_ms"] for r in recs if r.get("latency_ms"))
    if lat:
        rows.append(f"| latency p50 / p95 | {lat[len(lat) // 2]:.0f} / {lat[int(0.95 * (len(lat) - 1))]:.0f} ms | {len(lat)} |")
    by = defaultdict(list)
    for r in recs:
        by[r.get("style")].append(r)
    style = ["", "| style | n | intent | args | status |", "|---|---|---|---|---|"]
    style += [f"| {s} | {len(v)} | {_pct(_mean([float(x['intent_ok']) for x in v]))} | {_pct(_mean([float(x['args_ok']) for x in v]))} | "
              f"{_pct(_mean([float(x['status_ok']) for x in v]))} |" for s, v in sorted(by.items(), key=lambda kv: str(kv[0]))]
    # calibration of the router confidence threshold (starting value 0.6)
    cal = ["", "Router confidence threshold (answered = confidence ≥ t; correct = intent and arguments right):", "",
           "| t | answered | correct among answered |", "|---|---|---|"]
    best = None
    for t in [x / 10 for x in range(3, 10)]:
        ans = [r for r in recs if (r.get("confidence") or 0) >= t]
        acc = _mean([float(r["intent_ok"] and r["args_ok"]) for r in ans])
        cal.append(f"| {t:.1f} | {_pct(len(ans) / len(recs))} | {_pct(acc)} |")
        if best is None and acc is not None and acc >= 0.95:
            best = t
    cal.append(f"\nLowest threshold with ≥ 95 % correct among answered: **{best if best is not None else 'none'}** "
               "(set `router.confidence_threshold` in config/models.yaml).")
    return "\n".join(rows + style + cal)


def analytics_section() -> tuple[str, list[dict]]:
    table = ["| mode | evidence | n | BIRD EX (set) | multiset EX | SQLAS score (mean) |", "|---|---|---|---|---|---|"]
    fails, any_run = [], False
    for mode in ("fast", "full"):
        for ev in ("with", "without"):
            recs = [r for r in latest(load_jsonl(RES / f"analytics_{mode}_{ev}_evidence.jsonl")) if "bird_ex" in r]
            if not recs:
                table.append(f"| {mode} | {ev} | 0 | not run | | |")
                continue
            any_run = True
            table.append(f"| {mode} | {ev} | {len(recs)} | {_pct(_mean([r['bird_ex'] for r in recs]))} | "
                         f"{_pct(_mean([r['multiset_ex'] for r in recs]))} | {_mean([r.get('sqlas_score') for r in recs]) or 0:.3f} |")
            fails += [dict(r, mode=mode, evidence=ev) for r in recs if not r["bird_ex"]]
    if not any_run:
        return "_Not run yet (`scripts/eval_local.sh`)._", []
    diff = ["", "By difficulty (fast mode, with evidence):", "", "| difficulty | n | BIRD EX |", "|---|---|---|"]
    by = defaultdict(list)
    for r in latest(load_jsonl(RES / "analytics_fast_with_evidence.jsonl")):
        if "bird_ex" in r:
            by[r.get("difficulty")].append(r["bird_ex"])
    diff += [f"| {d} | {len(v)} | {_pct(_mean(v))} |" for d, v in sorted(by.items(), key=lambda kv: str(kv[0]))]
    cats = Counter(f["failure_category"] for f in fails)
    cat = ["", "Failure categories (SQLAS `classify_failure`, all runs):", "", "| category | count |", "|---|---|"]
    cat += [f"| {c} | {n} |" for c, n in cats.most_common()]
    return "\n".join(table + diff + cat), fails


def examples(fails: list[dict]) -> str:
    if not fails:
        return "_No failures recorded (or not run)._"
    out, seen = [], Counter()
    for f in sorted(fails, key=lambda f: (f["failure_category"], str(f["id"]))):
        if seen[f["failure_category"]] >= 2:
            continue
        seen[f["failure_category"]] += 1
        out.append(f"**{f['failure_category']}** — q{f['id']} ({f['mode']}, {f['evidence']} evidence): {f['question']}\n\n"
                   f"```sql\n-- generated\n{f.get('pred_sql') or '(none: ' + (f.get('engine_error') or '')[:150] + ')'}\n"
                   f"-- gold\n{f['gold_sql']}\n```")
    return "\n\n".join(out)


def latency_section() -> str:
    out = []
    fake = RES / "latency_report.json"
    if fake.exists():
        r = json.loads(fake.read_text())
        a, b = r["after"], r["before"]
        out.append(f"Pipeline without models (FakeLLM, `eval/latency.py`): request mean **{a['stages_ms']['total']['mean']} ms** "
                   f"(before optimisations {b['stages_ms']['total']['mean']} ms), p95 {a['stages_ms']['total']['p95']} ms; "
                   f"LLM calls per analytics question {b['per_route']['analytics']['llm_calls_per_question']} → "
                   f"{a['per_route']['analytics']['llm_calls_per_question']}. Details: docs/LATENCY.md.")
    real = RES / "latency_real.json"
    if real.exists():
        r = json.loads(real.read_text())
        st = r["stages_ms"]
        out += ["", f"With the real models ({r['turns']} chat-smoke turns):", "", "| stage | p50 ms | p95 ms | n |", "|---|---|---|---|"]
        out += [f"| {k} | {v['p50']} | {v['p95']} | {v['n']} |" for k, v in st.items()
                if k in ("total", "router", "rewrite", "llm", "mercer_candidate_generation", "mercer_correction", "summary", "db_execute", "validation")]
        out += ["", "| LLM stage | prompt tok p50 | cached tok p50 | completion tok p50 | prefill ms p50 | decode ms p50 | decode tok/s p50 |",
                "|---|---|---|---|---|---|---|"]
        for s, f in r["llm_by_stage"].items():
            g = lambda k: f.get(k, {}).get("p50", "–")  # noqa: E731
            out.append(f"| {s} | {g('prompt_tokens')} | {g('cached_tokens')} | {g('completion_tokens')} | {g('prefill_ms')} | "
                       f"{g('decode_ms')} | {g('decode_tok_s')} |")
    else:
        out.append("\n_Real-model latency not run yet._")
    return "\n".join(out)


def chat_section() -> str:
    p = RES / "chat_smoke_real_summary.json"
    if not p.exists():
        return "_Real-model chat smoke not run yet._"
    r = json.loads(p.read_text())
    lines = [f"{r['conversations']} conversations / {r['turns']} turns via `/v1/ask`: status {r['status']}, routes {r['route']}. "
             f"{r['as_expected']}/{r['turns']} matched the fixture-based expectation (expected to differ on real data)."]
    if r["unexpected"]:
        lines += ["", "| conv | turn | expected | got | reason |", "|---|---|---|---|---|"]
        lines += [f"| {c} | {t} | {e} | {g} | {why} |" for c, t, e, g, why in r["unexpected"][:15]]
    return "\n".join(lines)


def spot_check() -> str:
    rng = random.Random(2026)
    pool = [("analytics", r) for f in RES.glob("analytics_*_evidence.jsonl") for r in latest(load_jsonl(f)) if "bird_ex" in r]
    pool += [("lookup", r) for r in latest(load_jsonl(RES / "lookup.jsonl"))]
    if not pool:
        return "_Nothing to spot-check yet._"
    pick = rng.sample(pool, min(10, len(pool)))
    rows = ["Checked by a person: compare the recorded verdict with the SQL / rows; tick one box per row.", "",
            "| # | kind | id | question | recorded verdict | scored correctly? |", "|---|---|---|---|---|---|"]
    for i, (kind, r) in enumerate(pick, 1):
        verdict = (f"BIRD EX {r['bird_ex']}" if kind == "analytics" else
                   f"intent {r['intent_ok']}, args {r['args_ok']}, status {r['status']}")
        rows.append(f"| {i} | {kind} | {r['id']} | {r['question'][:80]} | {verdict} | ☐ yes ☐ no |")
    return "\n".join(rows)


def main() -> None:
    analytics, fails = analytics_section()
    md = f"""# BankQL — report

Generated {date.today().isoformat()} by `eval/make_report.py` from `eval/results/`. Hardware, models and data for
each run are in the result files; numbers below are only what was measured — "not run" means no result yet.

## What was built

A read-only natural-language → SQL service for bank data on local models (llama.cpp): REST API (primary), Python
library, MCP server and OpenAI-compatible endpoint; deterministic lookups with fixed SQL; analytics through a ported
Mercer pipeline on Arctic-Text2SQL-R1-7B; sqlglot validator with PII lineage, the PII-filter permission and the
small-count rule; per-key policy; audit log. See README.md, docs/CONNECT.md, docs/SECURITY.md, docs/LATENCY.md.

## Architecture

{ARCH}

## Lookup accuracy (Phase 3, router = Gemma)

{lookup_section()}

## BIRD financial (Phase 4, SQL = Arctic via Mercer)

Headline = BIRD's official execution accuracy (rows compared as sets, `bird-bench/mini_dev` `calculate_ex`).
"with evidence" passes BIRD's hint; "without" does not.

{analytics}

### Failure examples

{examples(fails)}

## Chat smoke test (real models)

{chat_section()}

## Latency

{latency_section()}

## Spot-check

{spot_check()}

## Known limitations

- Differencing attacks on PII counts (keys with `pii_filter` only); per-user identity not yet enforced — docs/SECURITY.md.
- Rate limiter and sessions are per process; the verified-query cache is per instance.
- Dialects beyond SQLite / DuckDB / PostgreSQL are untested (driver + config only).
- Summaries are model text checked for numbers, contacts and instructions — treat them as untrusted in the calling app.

## Next steps

- Calibrate `router.confidence_threshold` from the table above; tune `sql.max_tokens` against truncated answers.
- Compare `link_schema: auto` vs `always` and fast vs full mode on this hardware.
- Per-user identity → database roles; query budgets for PII-filter keys.
"""
    (ROOT / "REPORT.md").write_text(md)
    print(ROOT / "REPORT.md")


if __name__ == "__main__":
    main()
