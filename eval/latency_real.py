"""Latency with the real models: per-stage p50/p95 and, per LLM call stage, tokens, cached-prefix tokens and
llama-server's prefill vs decode times — from a real-model chat-smoke run (eval/chat_smoke.py --url …).

    .venv/bin/python eval/latency_real.py eval/results/chat_smoke_real.jsonl   # -> eval/results/latency_real.json
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bankql.eval import load_jsonl  # noqa: E402
from eval.chat_smoke import pct  # noqa: E402


def summarise(recs: list[dict]) -> dict:
    stages, calls = defaultdict(list), defaultdict(lambda: defaultdict(list))
    for r in recs:
        for k, v in (r.get("timings_ms") or {}).items():
            stages[k].append(v)
        for c in r.get("llm_calls") or []:
            for f in ("prompt_tokens", "completion_tokens", "cached_tokens", "prefill_ms", "decode_ms", "decode_tok_s", "wall_ms"):
                if c.get(f) is not None:
                    calls[c["stage"]][f].append(c[f])
    q = lambda v: {"p50": pct(v, .5), "p95": pct(v, .95), "n": len(v)}  # noqa: E731
    return {"turns": len(recs), "stages_ms": {k: q(v) for k, v in sorted(stages.items())},
            "llm_by_stage": {s: {f: q(v) for f, v in fs.items()} for s, fs in sorted(calls.items())}}


if __name__ == "__main__":
    src = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "eval/results/chat_smoke_real.jsonl")
    rep = summarise(load_jsonl(src))
    out = ROOT / "eval/results/latency_real.json"
    out.write_text(json.dumps(rep, indent=2))
    print(json.dumps(rep, indent=2))
