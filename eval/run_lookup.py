"""Phase 3: router + lookup evaluation. Resumable: rerun the same command to continue.

    .venv/bin/python eval/run_lookup.py --items eval/datasets/lookup.jsonl --source bank_demo --limit 10
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bankql import Agent  # noqa: E402
from bankql.eval import latest, load_jsonl, run_jsonl  # noqa: E402
from bankql.eval.leakage import leaks, prompt_corpus  # noqa: E402
from bankql.eval.metrics import summarize  # noqa: E402
from bankql.eval.tasks import lookup_task  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--items", required=True)
ap.add_argument("--source", default="bank_demo")
ap.add_argument("--out", default=str(ROOT / "eval/results/lookup.jsonl"))
ap.add_argument("--limit", type=int)
a = ap.parse_args()

items = load_jsonl(a.items)
bad = leaks(items, prompt_corpus(ROOT))
assert not bad, f"evaluation items leak into prompts: {bad}"
agent = Agent.from_config(ROOT / "config")
print(run_jsonl(items, lookup_task(agent, a.source), a.out, limit=a.limit))
print(json.dumps(summarize(latest(load_jsonl(a.out)), ["intent_ok", "args_ok", "rows_ok", "status_ok"]), indent=2))
