"""Metrics. Headline analytics metric = BIRD's official execution accuracy, reproduced exactly from
bird-bench/mini_dev evaluation/evaluation_ex.py @ 1c49a37:  1 if set(pred_rows) == set(gold_rows) else 0
(row order and duplicate rows ignored). Secondary: multiset-strict EX and the SQLAS score (sqlas==2.8.0
execution_accuracy, a weighted 0–1 score — not comparable to BIRD) with SQLAS failure classification."""
from __future__ import annotations

import sqlite3
import statistics
import time
from collections import Counter
from contextlib import closing

BIRD_TIMEOUT_S = 30.0  # mini_dev default meta_time_out


def bird_ex(pred_rows, gold_rows) -> int:
    return 1 if set(pred_rows) == set(gold_rows) else 0


def multiset_ex(pred_rows, gold_rows) -> int:
    return 1 if Counter(pred_rows) == Counter(gold_rows) else 0


def sqlite_fetch(db_path: str, sql: str, timeout_s: float = BIRD_TIMEOUT_S) -> list[tuple]:
    """fetchall() on a read-only connection (BIRD uses sqlite3.connect + fetchall; read-only here for safety)."""
    with closing(sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)) as con:
        deadline = time.monotonic() + timeout_s
        con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        return con.execute(sql).fetchall()


def score_analytics(pred_sql: str | None, gold_sql: str, db_path: str) -> dict:
    """Execute pred + gold on the ORIGINAL BIRD SQLite and score them. Errors/timeouts score 0, like BIRD."""
    gold = sqlite_fetch(db_path, gold_sql)
    rec = {"gold_rows": len(gold)}
    if not pred_sql:
        return {**rec, "bird_ex": 0, "multiset_ex": 0, "sqlas_score": 0.0, "pred_error": "no SQL",
                "failure_category": "no_sql"}
    try:
        pred = sqlite_fetch(db_path, pred_sql)
    except Exception as e:  # noqa: BLE001
        return {**rec, "bird_ex": 0, "multiset_ex": 0, "sqlas_score": 0.0, "pred_error": str(e)[:300],
                "failure_category": "execution_error"}
    rec.update(pred_rows=len(pred), bird_ex=bird_ex(pred, gold), multiset_ex=multiset_ex(pred, gold))
    rec.update(sqlas_hooks(pred_sql, gold_sql, db_path, rec["bird_ex"]))
    return rec


def sqlas_hooks(pred_sql: str, gold_sql: str, db_path: str, ex: int) -> dict:
    """SQLAS execution_accuracy (secondary) + classify_failure. No LLM judge needed for either."""
    from sqlas import classify_failure, execution_accuracy

    score, details = execution_accuracy(pred_sql, gold_sql, execute_fn=lambda s: sqlite_fetch(db_path, s))
    out = {"sqlas_score": score, "sqlas_details": {k: v for k, v in details.items() if k != "scalar_comparison"}}
    if ex:
        out["failure_category"] = "correct"
        return out
    # classify_failure reads scores / details keyed by metric name (sqlas/failure_analysis.py @ 2.8.0).
    scores = {"execution_accuracy": score, "row_count_match": details.get("count_ratio", 0.0),
              "empty_result_penalty": 0.0 if details.get("predicted_rows") == 0 and details.get("gold_rows") else 1.0}
    fa = classify_failure(pred_sql, scores, {
        "execution_accuracy": details,
        "row_count_match": {"pred_count": details.get("predicted_rows", 0), "gold_count": details.get("gold_rows", 0)}})
    # BIRD says wrong but SQLAS found nothing specific: keep it visible as 'wrong_result'.
    out["failure_category"] = "wrong_result" if fa.primary.value == "correct" else fa.primary.value
    out["failure_categories"] = [c.value for c in fa.categories]
    return out


# ------------------------------------------------------------------------------------------- lookup

def _norm_args(a: dict | None) -> dict:
    return {k: (str(v).strip().lower() if isinstance(v, str) else v) for k, v in (a or {}).items() if v is not None}


def score_lookup(item: dict, got: dict) -> dict:
    """item: {gold_route, gold_tools: [{tool, args}], expected_rows (first result set) | null,
    expect: ok|refused|needs_clarification}. got: a recorded AskResult-like dict."""
    gold_tools = [t["tool"] for t in item.get("gold_tools", [])]
    got_tools = got.get("router_tools", [])
    intent_ok = got.get("router_intent") == item["gold_route"] and [t["tool"] for t in got_tools] == gold_tools
    args_ok = intent_ok and all(_norm_args(g.get("args")) == _norm_args(h.get("args"))
                                for g, h in zip(item.get("gold_tools", []), got_tools))
    status_ok = got.get("status") == item["expect"]
    rows_ok = None
    if item["expect"] == "ok" and item.get("expected_rows") is not None:
        rows_ok = got.get("status") == "ok" and got.get("rows") == item["expected_rows"]
    return {"intent_ok": intent_ok, "args_ok": args_ok, "status_ok": status_ok, "rows_ok": rows_ok}


def summarize(records: list[dict], fields: list[str]) -> dict:
    """Accuracy per boolean/0-1 field with its sample size, plus latency percentiles."""
    out = {"n": len(records)}
    for f in fields:
        vals = [float(r[f]) for r in records if r.get(f) is not None]
        out[f] = {"acc": round(sum(vals) / len(vals), 4) if vals else None, "n": len(vals)}
    lat = sorted(r["latency_ms"] for r in records if r.get("latency_ms") is not None)
    if lat:
        out["latency_ms"] = {"p50": statistics.median(lat), "p90": lat[min(len(lat) - 1, int(0.9 * len(lat)))],
                             "max": lat[-1], "n": len(lat)}
    cats = Counter(r.get("failure_category") for r in records if r.get("failure_category"))
    if cats:
        out["failure_categories"] = dict(cats.most_common())
    return out
