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
}

if __name__ == "__main__":
    name = sys.argv[1]
    GATES[name]()
    print(f"GATE {name} OK")
