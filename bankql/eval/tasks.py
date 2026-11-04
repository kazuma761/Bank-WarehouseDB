"""Per-item evaluation tasks (used by eval/run_*.py and the tests)."""
from __future__ import annotations

import time

from bankql.eval.metrics import score_analytics, score_lookup


def analytics_task(engine, source, db_path: str, *, use_evidence: bool):
    """Analytics engine only (no router), scored on the ORIGINAL BIRD SQLite at db_path."""
    def run(item: dict) -> dict:
        t0 = time.perf_counter()
        ar = engine.generate(item["question"], source=source, evidence=item.get("evidence") if use_evidence else None)
        latency = round((time.perf_counter() - t0) * 1000, 1)
        return {"question": item["question"], "evidence_used": use_evidence, "difficulty": item.get("difficulty"),
                "mode": ar.mode, "pred_sql": ar.sql, "engine_ok": ar.ok, "engine_error": ar.error[:300],
                "corrections": ar.corrections, "stage_timings_ms": ar.stage_timings_ms, "latency_ms": latency,
                "gold_sql": item["gold_sql"], **score_analytics(ar.sql, item["gold_sql"], db_path)}
    return run


def lookup_task(agent, source: str):
    def run(item: dict) -> dict:
        r = agent.ask(item["question"], source=source, surface="eval")
        got = {"status": r.status, "route": r.route, "rows": r.rows, "router_intent": r.router.get("intent"),
               "router_tools": r.router.get("tools", []), "confidence": r.confidence,
               "self_confidence": r.router.get("self_confidence"), "logprob_confidence": r.router.get("logprob_confidence")}
        return {"question": item["question"], "style": item.get("style"), **got, "refusal_reason": r.refusal_reason,
                "latency_ms": r.timings_ms.get("total"), **score_lookup(item, got)}
    return run
