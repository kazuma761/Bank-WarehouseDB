import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bankql.config import load_config, load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")
os.environ.setdefault("BANKQL_FINANCIAL_URL", f"sqlite:///{ROOT}/data/fixture/financial.sqlite")
os.environ.setdefault("BANKQL_DEMO_URL", f"duckdb:///{ROOT}/data/bank_demo.duckdb")
# Absolute paths so tests don't depend on the working directory.
for k in ("BANKQL_FINANCIAL_URL", "BANKQL_DEMO_URL"):
    scheme, _, path = os.environ[k].partition(":///")
    if not path.startswith("/"):
        os.environ[k] = f"{scheme}:///{ROOT / path}"

SOURCES = ["financial", "bank_demo", "bank_pg"]


def _fixture_present() -> bool:
    return (ROOT / "data/fixture/financial.sqlite").exists() and (ROOT / "data/bank_demo.duckdb").exists()


@pytest.fixture(scope="session", autouse=True)
def fixture_db():
    if not _fixture_present():
        from scripts.make_fixture import generate, write_duckdb, write_postgres, write_sqlite, _pg_dsn

        data = generate()
        write_sqlite(data)
        write_duckdb(data)
        write_postgres(data, _pg_dsn(os.environ["BANKQL_PG_ADMIN_URL"]))


@pytest.fixture(scope="session")
def cfg():
    return load_config(ROOT / "config")


@pytest.fixture(scope="session")
def sources(cfg):
    from bankql.connectors import build_sources

    return build_sources(cfg)


@pytest.fixture(params=SOURCES)
def src(request, sources):
    return sources[request.param]


@pytest.fixture
def sqlite_src(sources):
    return sources["financial"]


@pytest.fixture
def make_agent(cfg, sources, tmp_path):
    """Agent on the real fixture DBs with FakeLLM for both the router and Mercer."""
    from bankql import Agent
    from bankql.analytics import MercerEngine
    from bankql.audit import AuditLog
    from bankql.export import Exporter
    from tests.fakes import mercer_fake

    def make(router_llm, sql_answers=None, fix=None, mode="fast"):
        eng = MercerEngine(mercer_fake(sql_answers or {}, fix=fix), mode=mode, work_dir=tmp_path / "m")
        from bankql.verified import VerifiedQueries

        return Agent(cfg, router_llm=router_llm, engine=eng, sources=dict(sources),
                     audit=AuditLog(tmp_path / "audit.duckdb"), exporter=Exporter(tmp_path / "exports"),
                     verified=VerifiedQueries(tmp_path / "verified.json"))
    return make
