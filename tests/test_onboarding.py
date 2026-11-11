"""Plug in data: CSV/Parquet import, connecting a database, removal, the /admin API (FakeLLM, temp config dir)."""
import os
import shutil

import pytest
import yaml
from fastapi.testclient import TestClient

from bankql.api import create_app
from bankql.config import load_config
from bankql.onboarding import OnboardError, env_var
from tests.conftest import ROOT
from tests.fakes import agent_fake, mercer_fake

SALES = "region,amount,customer_email\nnorth,10.5,a@x.org\nsouth,4,b@x.org\nnorth,1.5,c@x.org\n"
BY_REGION = "SELECT region, SUM(amount) AS total FROM sales GROUP BY region ORDER BY region"


@pytest.fixture
def plug(tmp_path, monkeypatch):
    """Agent on a copy of config/ in tmp_path; its router answers 'unsupported' to everything."""
    from bankql import Agent
    from bankql.analytics import MercerEngine
    from bankql.audit import AuditLog
    from bankql.export import Exporter
    from bankql.verified import VerifiedQueries

    shutil.copytree(ROOT / "config", tmp_path / "config")
    (tmp_path / "config" / "sources.local.yaml").unlink(missing_ok=True)
    shutil.rmtree(tmp_path / "config" / "semantic" / "local", ignore_errors=True)

    def make(sql=None):
        eng = MercerEngine(mercer_fake(sql or {}), mode="fast", work_dir=tmp_path / "m")
        return Agent(load_config(tmp_path / "config"), router_llm=agent_fake({}, summary="Totals by region."), engine=eng,
                     audit=AuditLog(tmp_path / "audit.duckdb"), exporter=Exporter(tmp_path / "exports"),
                     verified=VerifiedQueries(tmp_path / "verified.json"))
    yield make
    for k in [k for k in os.environ if k.startswith("BANKQL_SRC_")]:
        monkeypatch.delenv(k, raising=False)


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_import_csv_then_ask_skips_bank_router(plug, tmp_path):
    agent = plug({"region": BY_REGION})
    r = agent.import_file("sales", "Sales.csv", write(tmp_path, "up.csv", SALES))
    assert r.ok, r.checks
    assert r.tables == ["sales"] and r.semantic_files == ["semantic/local/sales/sales.yaml"]
    res = agent.ask("total amount by region", "sales")
    assert res.status == "ok", res.refusal_reason
    assert res.rows == [["north", 12.0], ["south", 4.0]]
    assert res.router["reason"] == "this source has no lookup tables"  # the fake router would have said unsupported
    assert not any(rs.legend for rs in res.results)  # skeleton placeholders ("north: TODO meaning") never shown
    assert agent.sources["sales"].semantic.table("sales").description == ""
    # persisted: a fresh config sees it, with the URL in .env (mode 600)
    cfg = load_config(tmp_path / "config")
    assert cfg.sources["sales"]["origin"] == "import"
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600
    assert f"{env_var('sales')}=duckdb:///" in (tmp_path / ".env").read_text()


def test_imported_pii_columns_are_flagged_and_blocked(plug, tmp_path):
    agent = plug()
    agent.import_file("sales", "sales.csv", write(tmp_path, "up.csv", SALES))
    sem = yaml.safe_load((tmp_path / "config/semantic/local/sales/sales.yaml").read_text())
    assert sem["tables"]["sales"]["columns"]["customer_email"]["pii"] is True
    assert "valid_values" not in sem["tables"]["sales"]["columns"]["customer_email"]  # never sampled
    v = agent.sources["sales"].validate("SELECT customer_email FROM sales")
    assert not v.ok and v.code == "pii_output"


def test_reimport_replaces_table_and_adds_new_ones(plug, tmp_path):
    agent = plug({"region": BY_REGION})
    agent.import_file("sales", "sales.csv", write(tmp_path, "a.csv", SALES))
    agent.ask("total amount by region", "sales")  # opens the DuckDB file and caches Mercer's schema
    r = agent.import_file("sales", "sales.csv", write(tmp_path, "b.csv", "region,amount,channel\neast,7,web\n"))
    assert r.ok, r.checks
    r = agent.import_file("sales", "targets.parquet", _parquet(tmp_path))
    assert r.ok and r.tables == ["sales", "targets"]
    src = agent.sources["sales"]
    assert src.run("SELECT region, channel FROM sales").rows == [["east", "web"]]
    assert src.run("SELECT COUNT(*) AS n FROM targets").rows == [[2]]
    assert agent.ask("total amount by region", "sales").rows == [["east", 7.0]]


def _parquet(tmp_path):
    import duckdb

    p = tmp_path / "t.parquet"
    duckdb.sql(f"COPY (SELECT * FROM (VALUES ('north', 100), ('south', 50)) t(region, target)) TO '{p}' (FORMAT parquet)")
    return p


@pytest.mark.parametrize("name,filename,err", [
    ("Sales!", "a.csv", "source name"), ("financial", "a.csv", "sources.yaml"), ("fed_dev", "a.csv", "sources.yaml"),
    ("sales", "a.xlsx", "unsupported file type"), ("sales", "noext", "unsupported file type"),
])
def test_bad_import_input(plug, tmp_path, name, filename, err):
    with pytest.raises(OnboardError, match=err):
        plug().import_file(name, filename, write(tmp_path, "up.csv", SALES))


def test_unreadable_file_is_a_clear_error(plug, tmp_path):
    with pytest.raises(OnboardError, match="could not read"):
        plug().import_file("sales", "x.parquet", write(tmp_path, "x.parquet", "not parquet"))


def test_connect_read_only_sqlite(plug):
    agent = plug()
    url = f"sqlite:///{ROOT}/data/fixture/financial.sqlite"
    r = agent.connect_source("mybank", url, tables=["loan", "district"])
    assert r.ok, r.checks
    assert agent.sources["mybank"].run("SELECT COUNT(*) AS n FROM loan").rows[0][0] > 0
    assert not agent.sources["mybank"].validate("SELECT * FROM client").ok  # not allowlisted


def test_connect_refuses_writable_postgres_role_and_saves_nothing(plug, tmp_path):
    agent = plug()
    r = agent.connect_source("pgadmin", os.environ["BANKQL_PG_ADMIN_URL"])  # bankql_admin can write
    assert not r.ok and any(s == "FAIL" for s, _, _ in r.checks)
    assert "pgadmin" not in agent.sources and env_var("pgadmin") not in os.environ
    assert not (tmp_path / "config/sources.local.yaml").exists()
    assert not list((tmp_path / "config/semantic/local/pgadmin").glob("*.yaml"))


def test_connect_unknown_table_and_bad_url(plug):
    agent = plug()
    r = agent.connect_source("mybank", f"sqlite:///{ROOT}/data/fixture/financial.sqlite", tables=["nope"])
    assert not r.ok and "not found: nope" in r.checks[0][2]
    with pytest.raises(OnboardError, match="SQLAlchemy URL"):
        agent.connect_source("mybank", "not a url")


def test_remove_source(plug, tmp_path):
    agent = plug()
    agent.import_file("sales", "sales.csv", write(tmp_path, "up.csv", SALES))
    assert agent.remove_source("sales")
    assert "sales" not in agent.sources and "sales" not in load_config(tmp_path / "config").sources
    assert env_var("sales") not in (tmp_path / ".env").read_text()
    assert not agent.remove_source("sales")
    with pytest.raises(OnboardError):
        agent.remove_source("financial")


def test_local_file_cannot_shadow_configured_source(plug, tmp_path):
    (tmp_path / "config/sources.local.yaml").write_text(yaml.safe_dump(
        {"sources": {"financial": {"url_env": "X", "dialect": "duckdb", "tables": ["x"], "semantic": []}}}))
    assert load_config(tmp_path / "config").sources["financial"]["url_env"] == "BANKQL_FINANCIAL_URL"


# ------------------------------------------------------------------------------------------------ REST /admin

ADMIN, USER = "admin-key-1", "user-key-1"


@pytest.fixture
def api(plug, monkeypatch):
    monkeypatch.setenv("BANKQL_API_KEYS", USER)
    monkeypatch.setenv("BANKQL_ADMIN_KEYS", ADMIN)
    return TestClient(create_app(plug({"region": BY_REGION})))


def test_admin_endpoints_need_admin_key(api):
    for method, path in (("GET", "/v1/admin/sources"), ("DELETE", "/v1/admin/sources/x"),
                         ("PUT", "/v1/admin/imports/sales/a.csv"), ("POST", "/v1/admin/sources/inspect")):
        assert api.request(method, path, headers={"X-API-Key": USER}, content=b"x").status_code == 403
        assert api.request(method, path, content=b"x").status_code == 401


def test_admin_page_is_static_and_locked_down(api):
    r = api.get("/admin")
    assert r.status_code == 200 and "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "innerHTML" not in r.text


def test_api_import_ask_remove(api):
    h = {"X-API-Key": ADMIN}
    r = api.put("/v1/admin/imports/sales/sales.csv", content=SALES.encode(), headers=h)
    assert r.status_code == 200 and r.json()["ok"] and r.json()["tables"] == ["sales"]
    assert {"name": "sales", "origin": "import"}.items() <= next(s for s in api.get("/v1/admin/sources", headers=h).json()
                                                                if s["name"] == "sales").items()
    ask = api.post("/v1/ask", json={"question": "total amount by region", "source": "sales"}, headers={"X-API-Key": USER})
    assert ask.json()["rows"] == [["north", 12.0], ["south", 4.0]]
    assert api.delete("/v1/admin/sources/sales", headers=h).json() == {"removed": "sales"}
    assert api.delete("/v1/admin/sources/sales", headers=h).status_code == 404


def test_api_import_limits(api, monkeypatch, tmp_path):
    h = {"X-API-Key": ADMIN}
    assert api.put("/v1/admin/imports/sales/a.xlsx", content=b"x", headers=h).status_code == 400
    assert api.put("/v1/admin/imports/Bad/a.csv", content=b"x", headers=h).status_code == 400
    assert api.put("/v1/admin/imports/sales/a.csv", content=b"", headers=h).status_code == 400
    monkeypatch.setenv("BANKQL_IMPORT_MAX_MB", "0.00001")
    assert api.put("/v1/admin/imports/sales/a.csv", content=SALES.encode(), headers=h).status_code == 413
    assert not list((tmp_path / "data/imports/.upload").iterdir())  # partial uploads are deleted


def test_api_inspect_and_connect(api):
    h = {"X-API-Key": ADMIN}
    url = f"sqlite:///{ROOT}/data/fixture/financial.sqlite"
    r = api.post("/v1/admin/sources/inspect", json={"name": "mybank", "url": url}, headers=h).json()
    assert "loan" in r["tables"] and all(c["status"] != "FAIL" for c in r["checks"])
    r = api.post("/v1/admin/sources", json={"name": "mybank", "url": url, "tables": ["loan"]}, headers=h)
    assert r.status_code == 200 and r.json()["tables"] == ["loan"]
    bad = api.post("/v1/admin/sources", json={"name": "pgadmin", "url": os.environ["BANKQL_PG_ADMIN_URL"]}, headers=h)
    assert bad.status_code == 400 and bad.json()["detail"]["ok"] is False
    assert os.environ["BANKQL_PG_ADMIN_URL"].split("@")[0] not in bad.text  # credentials never echoed


def test_cli_import_connect_remove(tmp_path, capsys, monkeypatch):
    import io

    from bankql.cli import main

    shutil.copytree(ROOT / "config", tmp_path / "config")
    (tmp_path / "config" / "sources.local.yaml").unlink(missing_ok=True)
    cfg = ["--config", str(tmp_path / "config")]
    assert main(cfg + ["import", "shop", str(write(tmp_path, "orders.csv", SALES))]) == 0
    assert "added source shop: orders" in capsys.readouterr().out
    monkeypatch.setattr("sys.stdin", io.StringIO(f"sqlite:///{ROOT}/data/fixture/financial.sqlite\n"))
    assert main(cfg + ["connect", "fx", "--tables", "loan"]) == 0
    assert set(load_config(tmp_path / "config").sources) >= {"shop", "fx"}
    assert main(cfg + ["import", "shop", "a.xlsx"]) == 2
    assert main(cfg + ["remove-source", "fx"]) == 0 and main(cfg + ["remove-source", "fx"]) == 1
    for k in [k for k in os.environ if k.startswith("BANKQL_SRC_")]:
        monkeypatch.delenv(k)
