import os

import pytest
import yaml

from bankql.cli import FAIL, PASS, check_source, init_source, main
from bankql.connectors import Source
from bankql.semantic import SemanticModel, load_semantic
from tests.conftest import ROOT


def test_check_source_passes_on_read_only_sources(src):
    results = check_source(src)
    assert all(s == PASS for s, _, _ in results), results
    assert {w for _, w, _ in results} >= {"connect", "allowlisted tables", "statement timeout"}


def test_check_source_fails_on_a_writable_postgres_role(sources):
    """Negative control: the admin role can write, and the inspection (not a write attempt) must say so."""
    pg = sources["bank_pg"]
    admin = Source("pg_admin", pg.cfg.model_copy(update={"url_env": "BANKQL_PG_ADMIN_URL"}), pg.semantic)
    fails = {w for s, w, _ in check_source(admin) if s == FAIL}
    assert {"postgres default_transaction_read_only", "postgres table write privileges"} <= fails


def test_check_source_reports_missing_tables(sources):
    s = sources["financial"]
    bad = Source("x", s.cfg.model_copy(update={"tables": s.cfg.tables + ["no_such_table"]}), s.semantic)
    with pytest.raises(RuntimeError):  # catalog refuses a missing, non-optional allowlisted table
        bad.catalog
    assert any(st == FAIL and w == "allowlisted tables" for st, w, _ in check_source(bad))


def test_check_source_cli_exit_codes(capsys):
    assert main(["--config", str(ROOT / "config"), "check-source", "financial"]) == 0
    assert main(["--config", str(ROOT / "config"), "check-source", "nope"]) == 2


@pytest.mark.parametrize("name", ["financial", "bank_pg", "bank_demo"])
def test_init_source_skeleton(sources, tmp_path, name):
    s = sources[name]
    raw = Source(name, s.cfg.model_copy(update={"semantic": []}), SemanticModel(name=name))
    out = init_source(raw, tmp_path / f"{name}.yaml")
    doc = yaml.safe_load(out.read_text())
    m = load_semantic([out])  # loads as a valid semantic model
    assert set(m.tables) >= {"trans", "loan", "client_profile"} and m.tables["trans"].time_column == "date"
    prof = {c.lower(): v for c, v in doc["tables"]["client_profile"]["columns"].items()}
    assert prof["full_name"].get("pii") and prof["phone_masked"].get("pii") and "valid_values" not in prof["full_name"]
    tt = {c.lower(): v for c, v in doc["tables"]["trans"]["columns"].items()}
    assert {"PRIJEM", "VYDAJ"} <= set(tt["type"]["valid_values"])
    assert "TODO" in out.read_text()
    if name != "bank_demo":
        assert ["disp.account_id", "account.account_id"] in doc["joins"]


def test_init_source_refuses_overwrite(tmp_path):
    out = tmp_path / "x.yaml"
    out.write_text("keep me")
    assert main(["--config", str(ROOT / "config"), "init-source", "financial", "-o", str(out)]) == 1
    assert out.read_text() == "keep me"
