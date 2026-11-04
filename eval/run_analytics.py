"""Phase 4: BIRD financial through the analytics engine. Resumable: rerun the same command to continue.

    .venv/bin/python eval/run_analytics.py --bird-dir data/bird --mode fast --evidence both --limit 5
Results: eval/results/analytics_<mode>_<with|without>_evidence.jsonl  (append-only; one line per question)
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bankql.analytics import build_engine  # noqa: E402
from bankql.config import load_config  # noqa: E402
from bankql.connectors import build_sources  # noqa: E402
from bankql.eval import latest, load_jsonl, run_jsonl  # noqa: E402
from bankql.eval.bird import load_bird  # noqa: E402
from bankql.eval.leakage import leaks, prompt_corpus  # noqa: E402
from bankql.eval.metrics import summarize  # noqa: E402
from bankql.eval.tasks import analytics_task  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--bird-dir", required=True, help="unzipped minidev package")
ap.add_argument("--source", default="bird_financial", help="source pointing at the ORIGINAL BIRD SQLite")
ap.add_argument("--mode", choices=["fast", "full"], default="fast")
ap.add_argument("--evidence", choices=["with", "without", "both"], default="both")
ap.add_argument("--ids", help="comma-separated question ids (e.g. a 10-question full-mode subset)")
ap.add_argument("--limit", type=int, help="process at most N new questions this run")
a = ap.parse_args()

cfg = load_config(ROOT / "config")
cfg.sql.mode = a.mode
items, db, broken = load_bird(a.bird_dir)
print(f"{len(items)} financial questions; {len(broken)} gold SQL failed to execute (excluded): {[b['id'] for b in broken]}")
bad = leaks(items, prompt_corpus(ROOT))
assert not bad, f"evaluation items leak into prompts: {bad}"
if a.ids:
    keep = {int(x) for x in a.ids.split(",")}
    items = [i for i in items if i["id"] in keep]
source = build_sources(cfg)[a.source]
engine = build_engine(cfg)
for ev in (["with", "without"] if a.evidence == "both" else [a.evidence]):
    out = ROOT / f"eval/results/analytics_{a.mode}_{ev}_evidence.jsonl"
    print(ev, "evidence:", run_jsonl(items, analytics_task(engine, source, str(db), use_evidence=ev == "with"), out, limit=a.limit))
    print(json.dumps(summarize(latest(load_jsonl(out)), ["bird_ex", "multiset_ex", "sqlas_score"]), indent=2))
