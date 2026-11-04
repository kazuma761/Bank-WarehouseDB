"""Acceptance-gate oracles for GATES.md (unlazy). Prints `GATE <name> OK` only after every assertion passes."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def sh(*cmd: str) -> str:
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()


def pytest(*args: str) -> None:
    r = subprocess.run([PY, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider", *args],
                       cwd=ROOT, capture_output=True, text=True)
    print(r.stdout[-4000:], r.stderr[-2000:])
    assert r.returncode == 0, f"pytest exited {r.returncode}"
    summary = r.stdout.strip().splitlines()[-1]
    assert "skipped" not in summary, f"tests were skipped: {summary}"
    assert re.search(r"\d+ passed", summary), summary


def layout() -> None:
    for p in ["bankql/agent.py", "bankql/router.py", "bankql/resolver.py", "bankql/tools", "bankql/analytics",
              "bankql/guardrails.py", "bankql/connectors", "bankql/semantic", "bankql/llm", "bankql/export.py",
              "bankql/api", "bankql/mcp_server.py", "config", "data", "eval", "scripts", "examples", "tests",
              "third_party", "SPEC.md", "PROGRESS.md"]:
        assert (ROOT / p).exists(), f"missing {p}"
    assert int(sh("git", "rev-list", "--count", "HEAD")) >= 1


def pins() -> None:
    doc = (ROOT / "docs/THIRD_PARTY.md").read_text()
    for name, path in [("mercer", "third_party/mercer"), ("bird_mini_dev", "third_party/bird_mini_dev")]:
        recorded = re.search(rf"{name} commit: `([0-9a-f]{{40}})`", doc)
        assert recorded, f"{name} hash not recorded in docs/THIRD_PARTY.md"
        assert sh("git", "-C", path, "rev-parse", "HEAD") == recorded.group(1), f"{name} HEAD != recorded pin"
        assert recorded.group(1) in sh("git", "submodule", "status", path), f"{name} not pinned as submodule"
    import sqlas
    lock = (ROOT / "uv.lock").read_text()
    assert re.search(rf'name = "sqlas"\nversion = "{re.escape(sqlas.__version__)}"', lock), "sqlas != lock"


def fixture() -> None:
    sys.path.insert(0, str(ROOT))
    from scripts.make_fixture import check_fixture
    check_fixture()


def llama() -> None:
    out = subprocess.run([str(ROOT / ".venv-llama/bin/python"), "-c",
                          "import llama_cpp, llama_cpp.server.app; print(llama_cpp.__version__)"],
                         capture_output=True, text=True)
    print(out.stdout, out.stderr[-500:])
    assert out.returncode == 0 and re.match(r"\d+\.\d+\.\d+", out.stdout)


def pushed() -> None:
    local = sh("git", "rev-parse", "HEAD")
    remote = subprocess.run(["git", "ls-remote", "origin", "refs/heads/bankql"], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert remote.returncode == 0 and remote.stdout.split()[:1] == [local], f"origin/bankql != HEAD ({remote.stderr.strip()[:160]})"


def demo() -> None:
    import socket
    import time

    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        port = so.getsockname()[1]
    srv = subprocess.Popen([PY, "-c", f"import sys,time; sys.path.insert(0,'.'); from bankql.config import load_dotenv; load_dotenv('.env');"
                            f"from eval.chat_smoke import serve_fake; b,k,s=serve_fake({port}); open('/dev/stdout','w').write(k+'\\n'); "
                            f"sys.stdout.flush(); time.sleep(120)"], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    try:
        key = srv.stdout.readline().strip()
        out = subprocess.run(["node", str(ROOT / "scripts/check_demo.cjs"), f"http://127.0.0.1:{port}", key], capture_output=True, text=True,
                             timeout=120, env={**__import__("os").environ, "NODE_PATH": subprocess.run(["npm", "root", "-g"], capture_output=True, text=True).stdout.strip()})
        print(out.stdout, out.stderr[-500:])
        assert out.returncode == 0 and "DEMO OK" in out.stdout
    finally:
        srv.kill()


def demo_scripts() -> None:
    subprocess.run(["bash", "-n", str(ROOT / "scripts/demo.sh")], check=True)
    pytest("tests/security/test_side_services.py")


def connect_doc() -> None:
    doc = (ROOT / "docs/CONNECT.md").read_text()
    need = ["user_id", "X-API-Key", "/health", "Retry-After", "/v1/results/", "```bash", "```python", "```js", "```java",
            "dialect: postgresql", "dialect: mysql", "dialect: mssql", "dialect: oracle", "dialect: sqlite", "dialect: duckdb",
            "CREATE ROLE bankql_ro", "CREATE USER 'bankql_ro'", "CREATE LOGIN bankql_ro", "GRANT READ ON", "chmod 0440",
            "large_tables", "large_scan_threshold_rows", "Timeouts", "Part 3"]
    need += [f"| {c} |" for c in ("400", "401", "403", "404", "408", "422", "429")]
    missing = [n for n in need if n not in doc]
    import re as _re
    used = set()
    for path in list((ROOT / "bankql").rglob("*.py")) + list((ROOT / "config").rglob("*.yaml")) + list((ROOT / "scripts").glob("*")) + [ROOT / "docker-compose.yml", ROOT / "Dockerfile"]:
        if path.is_file() and "mercer" not in str(path) and path.suffix != ".pyc":
            used |= set(_re.findall(r"\b(BANKQL_[A-Z_]+[A-Z])\b", path.read_text(errors="ignore")))
    undocumented = sorted(v for v in used if v not in doc and v not in ("BANKQL_PG",))
    assert not missing and not undocumented, f"missing {missing}; undocumented env vars {undocumented}"


def chat_smoke() -> None:
    import json

    r = subprocess.run([PY, str(ROOT / "eval/chat_smoke.py")], cwd=ROOT, capture_output=True, text=True, timeout=600)
    assert r.returncode == 0, r.stderr[-800:]
    rep = json.loads((ROOT / "eval/results/chat_smoke_fake_summary.json").read_text())
    recs = [json.loads(l) for l in (ROOT / "eval/results/chat_smoke_fake.jsonl").read_text().splitlines() if l.strip()]
    assert rep["conversations"] >= 30 and rep["as_expected"] == rep["turns"] == len(recs), (rep["conversations"], rep["as_expected"], rep["unexpected"])
    assert all({"route", "sql", "row_count", "refusal_reason", "timings_ms"} <= set(x) for x in recs)
    assert all(x["http_status"] == 200 for x in recs)
    print(f"{rep['conversations']} conversations, {rep['turns']} turns, all as expected")


def security() -> None:
    import re as _re

    r = subprocess.run([PY, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/security"], cwd=ROOT, capture_output=True, text=True)
    summary = r.stdout.strip().splitlines()[-1]
    print(summary)
    assert r.returncode == 0 and "skipped" not in summary and "failed" not in summary
    measured = int(_re.search(r"(\d+) passed", summary).group(1))
    doc = (ROOT / "docs/SECURITY.md").read_text()
    stated = int(_re.search(r"`tests/security/` — \*\*(\d+) passed", doc).group(1))
    assert measured == stated, f"SECURITY.md says {stated} passed, measured {measured}"
    assert "Residual risks" in doc


def latency() -> None:
    import json

    r = subprocess.run([PY, str(ROOT / "eval/latency.py"), "--reps", "2"], cwd=ROOT, capture_output=True, text=True, timeout=900)
    assert r.returncode == 0, r.stderr[-800:]
    rep = json.loads((ROOT / "eval/results/latency_report.json").read_text())
    b, a = rep["before"], rep["after"]
    for x in (a, b):
        assert {"total", "validation", "db_execute", "input_guardrail", "router", "resolver", "output_guardrail"} <= set(x["stages_ms"])
        assert all({"p50", "p95"} <= set(v) for v in x["stages_ms"].values())
    assert a["stages_ms"]["total"]["mean"] < b["stages_ms"]["total"]["mean"], "after is not faster"
    assert a["per_route"]["analytics"]["llm_calls_per_question"] < b["per_route"]["analytics"]["llm_calls_per_question"]
    assert a["first_question_ms"] < b["first_question_ms"] and rep["profile_after"]["self_time_share"]
    assert "Native rewrite" in (ROOT / "docs/LATENCY.md").read_text()
    print(f"mean {b['stages_ms']['total']['mean']} -> {a['stages_ms']['total']['mean']} ms; analytics LLM calls "
          f"{b['per_route']['analytics']['llm_calls_per_question']} -> {a['per_route']['analytics']['llm_calls_per_question']}")


def docker() -> None:
    r = subprocess.run(["docker", "build", "-q", "-t", "bankql:gate", str(ROOT)], capture_output=True, text=True, timeout=1500)
    print(r.stdout[-300:], r.stderr[-600:])
    assert r.returncode == 0


def pins2() -> None:
    import re as _re

    import duckdb
    import yaml

    pin = _re.search(r'"duckdb==([\d.]+)"', (ROOT / "pyproject.toml").read_text())
    assert pin and pin.group(1) == duckdb.__version__, "duckdb not pinned to the installed version"
    later = (ROOT / "PROGRESS.md").read_text().split("## Later", 1)[-1].lower()
    assert "offline" in later and "extension" in later, "no Later note on offline extension files"
    m = yaml.safe_load((ROOT / "config/models.yaml").read_text())["router"]
    assert m["hf_repo"] and m["hf_file"], "Gemma GGUF not configured"


def all2() -> None:
    r = subprocess.run([PY, "-m", "pytest", "-q", "-rs", "-p", "no:cacheprovider"], cwd=ROOT, capture_output=True, text=True)
    out = r.stdout.strip().splitlines()
    print("\n".join(out[-4:]))
    skips = [l for l in out if l.startswith("SKIPPED")]
    assert r.returncode == 0 and len(skips) == 1 and "BLOCKED: DuckDB extension" in skips[0], skips


def bird() -> None:
    import os

    sys.path.insert(0, str(ROOT))
    from bankql.config import load_dotenv

    load_dotenv(ROOT / ".env")
    url = os.environ.get("BANKQL_BIRD_URL", "")
    assert url and Path(url.split(":///", 1)[-1]).exists(), "BIRD financial.sqlite not present (BANKQL_BIRD_URL)"
    from bankql.config import load_config
    from bankql.connectors import build_sources

    assert len(build_sources(load_config(ROOT / "config"))["bird_financial"].catalog.tables) == 8


GATES = {
    "layout": layout,
    "pins": pins,
    "fixture": fixture,
    "validator": lambda: pytest("tests/test_validator.py"),
    "core": lambda: pytest("tests/test_connectors.py", "tests/test_guardrails.py", "tests/test_pii_lineage.py",
                           "tests/test_resolver.py", "tests/test_tools.py", "tests/test_export.py",
                           "tests/test_audit.py"),
    "federation": lambda: pytest("tests/test_federation.py"),
    "semantic": lambda: pytest("tests/test_semantic.py"),
    "analytics": lambda: pytest("tests/test_analytics.py", "tests/test_no_gpu_imports.py"),
    "surfaces": lambda: pytest("tests/test_agent.py", "tests/test_api.py", "tests/test_mcp.py",
                               "tests/test_openai_compat.py"),
    "eval": lambda: pytest("tests/test_eval.py"),
    "llama": llama,
    "all": lambda: pytest("-m", "not real_llm"),
    "pushed": pushed, "demo": demo, "demo_scripts": demo_scripts, "connect_doc": connect_doc,
    "cli": lambda: pytest("tests/test_cli.py"), "chat_smoke": chat_smoke, "security": security, "latency": latency,
    "docker": docker, "pins2": pins2, "all2": all2, "bird": bird,
}

if __name__ == "__main__":
    name = sys.argv[1]
    GATES[name]()
    print(f"GATE {name} OK")
