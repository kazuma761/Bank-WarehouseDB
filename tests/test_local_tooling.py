"""Local GPU tooling that can be exercised without models or BIRD: BIRD loader (on a BIRD-shaped copy of the
fixture), lookup-set generator, REPORT.md generator, and serve_local.sh against a stand-in llama-server."""
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import textwrap

import pytest

from tests.conftest import ROOT


@pytest.fixture
def fake_minidev(tmp_path):
    root = tmp_path / "minidev" / "MINIDEV"
    (root / "dev_databases/financial").mkdir(parents=True)
    db = root / "dev_databases/financial/financial.sqlite"
    shutil.copy(ROOT / "data/fixture/financial.sqlite", db)
    con = sqlite3.connect(db)
    con.execute("DROP TABLE client_profile")  # like the real BIRD database
    con.commit()
    con.close()
    (root / "mini_dev_sqlite.json").write_text(json.dumps([
        {"question_id": 1, "db_id": "financial", "question": "q", "evidence": "", "SQL": "SELECT COUNT(*) FROM loan", "difficulty": "simple"}]))
    return tmp_path / "minidev", db


def test_load_bird_copies_without_touching_the_original(fake_minidev, tmp_path, monkeypatch):
    import scripts.load_bird as lb

    _, db = fake_minidev
    before = db.read_bytes()
    monkeypatch.setattr(lb, "DEMO_PATH", tmp_path / "bird_demo.duckdb")
    src = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    lb.check_schema(src)
    clients = [r[0] for r in src.execute("SELECT client_id FROM client")]
    prof = lb.profiles(clients)
    assert len(prof) == len(clients) and prof[1][1] == prof[0][1]  # planted duplicate names
    lb.copy_duckdb(src, prof, lb.DEMO_PATH)
    counts = lb.verify(db, None)
    assert counts["trans"] == 3590 and counts["client"] == 400
    assert db.read_bytes() == before  # original never written


def test_load_bird_rejects_unexpected_schema(fake_minidev):
    import scripts.load_bird as lb

    _, db = fake_minidev
    con = sqlite3.connect(db)
    con.execute("ALTER TABLE loan ADD COLUMN surprise INT")
    con.commit()
    with pytest.raises(SystemExit, match="loan"):
        lb.check_schema(con)


def test_lookup_set_has_60_items_all_styles_and_no_leakage(sources):
    from bankql.eval.leakage import leaks, prompt_corpus
    from eval.make_lookup_set import generate

    items = generate(sources["bank_demo"])
    styles = {i["style"] for i in items}
    assert len(items) == 60 and styles == {"formal", "casual", "typo", "hinglish", "dates", "name", "ambiguous_name",
                                           "period", "multi_tool", "missing_id"}
    assert leaks(items, prompt_corpus(ROOT)) == []
    assert all(i["expected_rows"] is not None for i in items if i["expect"] == "ok")
    assert {i["expect"] for i in items if i["style"] == "missing_id"} == {"refused"}
    assert {i["expect"] for i in items if i["style"] == "ambiguous_name"} == {"needs_clarification"}


def test_report_renders_every_section(tmp_path, monkeypatch):
    import eval.make_report as mr

    monkeypatch.setattr(mr, "RES", tmp_path)
    monkeypatch.setattr(mr, "ROOT", tmp_path)
    w = lambda n, recs: (tmp_path / n).write_text("".join(json.dumps(r) + "\n" for r in recs))  # noqa: E731
    w("lookup.jsonl", [{"id": f"l{i}", "question": "q", "style": "formal", "intent_ok": True, "args_ok": i % 3 > 0,
                        "rows_ok": True, "status_ok": True, "status": "ok", "confidence": i / 10, "latency_ms": 900} for i in range(10)])
    w("analytics_fast_with_evidence.jsonl", [{"id": i, "question": "q", "difficulty": "simple", "bird_ex": i % 2, "multiset_ex": i % 2,
                                              "sqlas_score": 0.5, "failure_category": "wrong_result", "pred_sql": "SELECT 1",
                                              "gold_sql": "SELECT 2"} for i in range(4)])
    mr.main()
    txt = (tmp_path / "REPORT.md").read_text()
    for must in ("intent accuracy", "Lowest threshold", "| fast | with | 4 |", "| full | without | 0 | not run",
                 "wrong_result", "```mermaid", "☐ yes ☐ no", "Real-model latency not run yet"):
        assert must in txt, must


STAND_IN = textwrap.dedent('''\
    #!/usr/bin/env python3
    import http.server, json, os, sys
    a = sys.argv[1:]; arg = lambda k: a[a.index(k) + 1]
    open(os.environ["FAKE_LOG"], "a").write(json.dumps(a) + "\\n")
    key = arg("--api-key")
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            ok = self.headers.get("Authorization") == f"Bearer {key}"
            self.send_response(200 if ok else 401); self.end_headers(); self.wfile.write(b"{}")
        def log_message(self, *x): pass
    http.server.HTTPServer((arg("--host"), int(arg("--port"))), H).serve_forever()
''')


def test_serve_local_flags_auth_and_stop(tmp_path):
    exe = tmp_path / "llama-server"
    exe.write_text(STAND_IN)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    for f in ("g.gguf", "a.gguf"):
        (tmp_path / f).touch()
    env = {**os.environ, "LLAMA_SERVER": str(exe), "FAKE_LOG": str(tmp_path / "argv.log"),
           "BANKQL_ROUTER_GGUF": str(tmp_path / "g.gguf"), "BANKQL_SQL_GGUF": str(tmp_path / "a.gguf"),
           "BANKQL_LLAMA_API_KEY": "stand-in-key"}
    run = lambda *a: subprocess.run([str(ROOT / "scripts/serve_local.sh"), *a], cwd=ROOT, env=env,  # noqa: E731
                                    capture_output=True, text=True, timeout=120)
    r = run("--detach")
    try:
        assert r.returncode == 0, r.stdout + r.stderr
        argv = [json.loads(l) for l in (tmp_path / "argv.log").read_text().splitlines()]
        by_port = {a[a.index("--port") + 1]: a for a in argv}
        for a in by_port.values():
            assert a[a.index("--host") + 1] == "127.0.0.1" and a[a.index("--api-key") + 1] == "stand-in-key"
        sql = by_port["8081"]
        assert sql[sql.index("-np") + 1] == "3" and sql.count("q8_0") == 2 and "-fa" in sql and sql[sql.index("-c") + 1] == "24576"
        import httpx

        assert httpx.get("http://127.0.0.1:8081/v1/models").status_code == 401
        assert httpx.get("http://127.0.0.1:8081/v1/models", headers={"Authorization": "Bearer stand-in-key"}).status_code == 200
    finally:
        run("--stop")
    import httpx

    with pytest.raises(httpx.ConnectError):
        httpx.get("http://127.0.0.1:8081/v1/models", timeout=2)


def test_local_scripts_parse():
    for name in ("setup_local.sh", "serve_local.sh", "eval_local.sh", "demo.sh", "setup_postgres.sh", "pg_init.sh", "fetch_models.sh"):
        assert subprocess.run(["bash", "-n", str(ROOT / "scripts" / name)]).returncode == 0, name
