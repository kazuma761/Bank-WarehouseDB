"""C: latency of the BankQL pipeline without models (FakeLLM), before vs after the optimisations.

    .venv/bin/python eval/latency.py --reps 3          # -> eval/results/latency_report.json + docs/LATENCY.md

before: Mercer schema linking (2 extra LLM calls), no verified-query cache, cold start.
after : link_schema=auto (full schema, constant prompt prefix), verified-query cache, warm-up at start.
cache_prompt and the Arctic token cap only change real-model time: they are reported from llama-server's
`timings` when run against real models (AskResult.llm_calls), not measurable with FakeLLM.
"""
from __future__ import annotations

import argparse
import cProfile
import io
import json
import pstats
import statistics
import sys
import tempfile
import time
import uuid
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bankql.config import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
from bankql.verified import VerifiedQueries  # noqa: E402
from eval.chat_smoke import CONVERSATIONS, fake_agent, pct  # noqa: E402

DB_SPANS = ("db_execute", "explain")
STAGES = ["input_guardrail", "rewrite", "router", "resolver", "mercer_entity_retrieval", "mercer_schema_linking",
          "mercer_candidate_generation", "validation", "explain", "db_execute", "mercer_execution_scoring",
          "mercer_correction", "execute", "summary", "output_guardrail", "llm", "total"]


def workload(agent, source: str, reps: int) -> list[dict]:
    out = []
    for rep in range(reps):
        for c in CONVERSATIONS:
            sid = f"lat-{rep}-{c['id']}-{uuid.uuid4().hex[:4]}"
            for t in c["turns"]:
                r = agent.ask(t["q"], source=source, session_id=sid)
                out.append({"rep": rep, "route": r.route, "status": r.status, "timings": r.timings_ms,
                            "llm_calls": r.llm_calls, "cache_hit": "verified_query_cache_hit" in r.warnings})
    return out


def summarise(recs: list[dict]) -> dict:
    stage = {s: [r["timings"][s] for r in recs if s in r["timings"]] for s in STAGES}
    total = sum(r["timings"]["total"] for r in recs)
    db = sum(sum(r["timings"].get(k, 0) for k in DB_SPANS) for r in recs)
    llm = sum(r["timings"].get("llm", 0) for r in recs)
    by_route = defaultdict(list)
    for r in recs:
        by_route[r["route"]].append(r)
    calls = {k: {"llm_calls_per_question": round(statistics.mean(len(x["llm_calls"]) for x in v), 2),
                 "prompt_chars_per_question": round(statistics.mean(sum(c["prompt_chars"] or 0 for c in x["llm_calls"]) for x in v)),
                 "n": len(v)} for k, v in by_route.items()}
    return {"n": len(recs), "first_question_ms": recs[0]["timings"]["total"],
            "stages_ms": {k: {"p50": pct(v, .5), "p95": pct(v, .95), "mean": round(statistics.mean(v), 2), "n": len(v)}
                          for k, v in stage.items() if v},
            "share": {"llm_fake": round(llm / total, 3), "database": round(db / total, 3),
                      "bankql_python": round((total - llm - db) / total, 3)},
            "per_route": calls, "cache_hits": sum(r["cache_hit"] for r in recs)}


def profile(agent, source: str) -> dict:
    prof = cProfile.Profile()
    prof.enable()
    workload(agent, source, 1)
    prof.disable()
    st = pstats.Stats(prof)
    groups: dict[str, float] = defaultdict(float)
    tot = 0.0
    for (file, _, fn), (_, _, tt, _, _) in st.stats.items():
        tot += tt
        g = ("mercer_port" if "analytics/mercer" in file else "bankql" if "/bankql/" in file else
             "sqlglot" if "sqlglot" in file else "sqlalchemy" if "sqlalchemy" in file else
             "db_drivers" if any(x in file for x in ("sqlite3", "duckdb", "psycopg")) or fn in ("execute", "fetchmany") and "~" in file else
             "sklearn/numpy/rapidfuzz" if any(x in file for x in ("sklearn", "numpy", "scipy", "rapidfuzz", "rank_bm25")) else
             "pydantic" if "pydantic" in file else "other")
        groups[g] += tt
    buf = io.StringIO()
    pstats.Stats(prof, stream=buf).sort_stats("tottime").print_stats(15)
    top = [ln.strip() for ln in buf.getvalue().splitlines() if ln.strip() and ln.strip()[0].isdigit()][:15]
    return {"self_time_share": {k: round(v / tot, 3) for k, v in sorted(groups.items(), key=lambda kv: -kv[1])},
            "top_tottime": top}


def build(label: str, tmp: Path):
    if label == "before":
        return fake_agent(link_schema="always")
    a = fake_agent(link_schema="auto", verified=VerifiedQueries(tmp / "verified.json"))
    a.warm()
    return a


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--source", default="bank_demo")
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp())
    report = {"source": a.source, "reps": a.reps, "turns_per_rep": sum(len(c["turns"]) for c in CONVERSATIONS)}
    for label in ("before", "after"):
        t0 = time.perf_counter()
        report[label] = summarise(workload(build(label, tmp), a.source, a.reps))
        report[label]["wall_s"] = round(time.perf_counter() - t0, 2)
    report["profile_after"] = profile(build("after", tmp / "p"), a.source)
    out = ROOT / "eval/results/latency_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
