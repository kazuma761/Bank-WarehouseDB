"""Evaluation harness: BIRD-faithful EX (checked against the official mini_dev code), resumable JSONL runs,
SQLAS hooks, harness end to end on fixture questions with FakeLLM."""
import json
import sqlite3
import sys
import types
from pathlib import Path

import pytest

from bankql.analytics import MercerEngine
from bankql.eval import latest, load_jsonl, run_jsonl
from bankql.eval.bird import load_bird
from bankql.eval.leakage import leaks, prompt_corpus
from bankql.eval.metrics import bird_ex, multiset_ex, score_analytics, summarize
from bankql.eval.tasks import analytics_task, lookup_task
from tests.fakes import agent_fake, mercer_fake, route

ROOT = Path(__file__).resolve().parents[1]
FX = load_jsonl(ROOT / "eval/fixtures/analytics_fixture.jsonl")
DB = str(ROOT / "data/fixture/financial.sqlite")


@pytest.fixture(scope="module")
def official_calculate_ex():
    sys.dont_write_bytecode = True  # keep the pinned submodule checkout clean
    for m in ("psycopg2", "pymysql"):  # imported at module top by evaluation_utils, unused for SQLite
        sys.modules.setdefault(m, types.ModuleType(m))
    sys.path.insert(0, str(ROOT / "third_party/bird_mini_dev/evaluation"))
    import evaluation_ex

    return evaluation_ex.calculate_ex


CASES = [
    ([(1,), (2,)], [(2,), (1,)]),            # order ignored
    ([(1,), (1,), (2,)], [(2,), (1,)]),      # duplicates ignored (set semantics)
    ([(1, 2)], [(2, 1)]),                    # column order matters
    ([(1.0,)], [(1,)]),                      # 1.0 == 1
    ([(None,)], [(None,)]),
    ([], []),
    ([(1,)], []),
    ([("a",)], [("A",)]),
    ([(3.14159,)], [(3.14,)]),
]


@pytest.mark.parametrize("pred,gold", CASES)
def test_bird_ex_matches_official(official_calculate_ex, pred, gold):
    assert bird_ex(pred, gold) == official_calculate_ex(pred, gold)


def test_multiset_is_stricter_than_bird():
    assert bird_ex([(1,), (1,)], [(1,)]) == 1 and multiset_ex([(1,), (1,)], [(1,)]) == 0


def test_score_analytics_correct_and_wrong():
    gold = FX[0]["gold_sql"]
    ok = score_analytics("SELECT COUNT(loan_id) FROM loan WHERE status = 'D'", gold, DB)
    assert ok["bird_ex"] == 1 and ok["failure_category"] == "correct" and 0 < ok["sqlas_score"] <= 1
    bad = score_analytics("SELECT COUNT(*) FROM loan", gold, DB)
    assert bad["bird_ex"] == 0 and bad["failure_category"] not in ("correct", None)
    err = score_analytics("SELECT nope FROM loan", gold, DB)
    assert err["bird_ex"] == 0 and err["failure_category"] == "execution_error"
    assert score_analytics(None, gold, DB)["failure_category"] == "no_sql"


def test_runner_resumes_without_redoing(tmp_path):
    out, calls = tmp_path / "r.jsonl", []
    items = [{"id": i} for i in range(5)]

    def fn(it):
        calls.append(it["id"])
        if it["id"] == 3 and calls.count(3) == 1:
            raise RuntimeError("flaky")
        return {"v": it["id"] * 10}

    s1 = run_jsonl(items, fn, out, limit=3)
    assert s1["ran"] == 3 and calls == [0, 1, 2]
    with out.open("a") as fh:
        fh.write('{"id": 99, "v": ')  # torn line from a killed run
    s2 = run_jsonl(items, fn, out)
    assert s2["skipped"] == 3 and calls == [0, 1, 2, 3, 4] and s2["errors"] == 1
    s3 = run_jsonl(items, fn, out)  # the errored item is retried, nothing else
    assert calls[-1] == 3 and s3["ran"] == 1
    recs = latest(load_jsonl(out))
    assert sorted(r["id"] for r in recs if "v" in r) == [0, 1, 2, 3, 4]


def test_analytics_harness_end_to_end(sqlite_src, tmp_path):
    answers = {it["question"]: it["gold_sql"] for it in FX[:4]}
    answers[FX[4]["question"]] = "SELECT SUM(amount) FROM trans WHERE type = 'VYDAJ' AND date >= '1998-12-01'"  # wrong
    engine = MercerEngine(mercer_fake(answers), work_dir=tmp_path / "m")
    out = tmp_path / "analytics.jsonl"
    run_jsonl(FX, analytics_task(engine, sqlite_src, DB, use_evidence=True), out)
    recs = latest(load_jsonl(out))
    s = summarize(recs, ["bird_ex", "multiset_ex", "sqlas_score"])
    assert s["n"] == 5 and s["bird_ex"] == {"acc": 0.8, "n": 5}
    assert s["failure_categories"]["correct"] == 4 and sum(s["failure_categories"].values()) == 5
    assert all("schema_linking" in r["stage_timings_ms"] and r["evidence_used"] for r in recs)


def test_lookup_harness_end_to_end(make_agent, sources):
    src = sources["bank_demo"]
    items = [
        {"id": "l1", "question": "show client 12's accounts", "gold_route": "lookup", "expect": "ok",
         "gold_tools": [{"tool": "get_accounts", "args": {"client_id": 12}}]},
        {"id": "l2", "question": "rahul sharma ke cards", "gold_route": "lookup", "expect": "needs_clarification",
         "gold_tools": [{"tool": "get_cards", "args": {"client_name": "Rahul Sharma"}}], "expected_rows": None},
        {"id": "l3", "question": "who is client 99999", "gold_route": "lookup", "expect": "refused",
         "gold_tools": [{"tool": "get_client", "args": {"client_id": 99999}}], "expected_rows": None},
    ]
    from bankql.tools import LookupTools
    items[0]["expected_rows"] = [[v.isoformat() if hasattr(v, "isoformat") else v for v in r] for r in LookupTools(src).get_accounts(12).rows]
    agent = make_agent(agent_fake({
        "client 12": route(tools=[{"tool": "get_accounts", "client_id": 12}]),
        "rahul": route(tools=[{"tool": "get_cards", "client_name": "rahul sharma"}]),
        "99999": route(tools=[{"tool": "get_client", "client_id": 99999}])}))
    recs = [{"id": it["id"], **lookup_task(agent, "bank_demo")(it)} for it in items]
    assert all(r["intent_ok"] and r["args_ok"] and r["status_ok"] for r in recs), recs
    assert recs[0]["rows_ok"] is True
    s = summarize(recs, ["intent_ok", "args_ok", "rows_ok", "status_ok"])
    assert s["rows_ok"] == {"acc": 1.0, "n": 1} and s["latency_ms"]["n"] == 3


def test_bird_loader_inspects_layout_and_verifies_gold(tmp_path):
    root = tmp_path / "minidev" / "MINIDEV"
    (root / "dev_databases/financial").mkdir(parents=True)
    db = root / "dev_databases/financial/financial.sqlite"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE loan (loan_id INTEGER, status TEXT)")
    con.commit()
    con.close()
    (root / "mini_dev_sqlite.json").write_text(json.dumps([
        {"question_id": 1, "db_id": "financial", "question": "q1", "evidence": "e", "SQL": "SELECT COUNT(*) FROM loan", "difficulty": "simple"},
        {"question_id": 2, "db_id": "financial", "question": "q2", "evidence": "", "SQL": "SELECT nope FROM loan", "difficulty": "simple"},
        {"question_id": 3, "db_id": "california_schools", "question": "q3", "evidence": "", "SQL": "SELECT 1", "difficulty": "simple"},
    ]))
    items, path, broken = load_bird(tmp_path / "minidev")
    assert [i["id"] for i in items] == [1] and [b["id"] for b in broken] == [2] and path == db


def test_leakage_guard(tmp_path):
    corpus = prompt_corpus(ROOT)
    assert leaks(FX, corpus) == []
    planted = [{"id": "x", "question": "You route questions for a bank's read-only data assistant", "gold_sql": ""}]
    assert leaks(planted, corpus) == [{"id": "x", "field": "question"}]  # positive control
