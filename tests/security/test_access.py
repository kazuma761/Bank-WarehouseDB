"""D3: per-key source allowlists, run_sql off by default, export ownership + expiry, no internals in errors."""
import time

import pytest
from fastapi.testclient import TestClient

from bankql.api import create_app
from bankql.auth import KeyStore, hash_key
from tests.fakes import agent_fake, mercer_fake, route


@pytest.fixture
def two_keys(make_agent, tmp_path, monkeypatch):
    monkeypatch.setenv("BANKQL_API_KEYS", "")
    (tmp_path / "api_keys.yaml").write_text(
        f"keys:\n  - {{id: app, sha256: {hash_key('app-key')}, sources: [bank_pg]}}\n"
        f"  - {{id: other, sha256: {hash_key('other-key')}, sources: [bank_pg]}}\n")
    agent = make_agent(agent_fake({"in debt": route("analytics"), "zzq everything": route("analytics")}, summary="ok"),
                       {"zzq everything": "SELECT nope FROM loan",
                        "loans are in debt": "SELECT loan_id, amount FROM loan WHERE status IN ('C', 'D')"},
                       fix="SELECT nope FROM loan")
    return TestClient(create_app(agent, keys=KeyStore(tmp_path))), agent


A, B = {"X-API-Key": "app-key"}, {"X-API-Key": "other-key"}


def test_source_allowlist(two_keys):
    c, _ = two_keys
    assert [s["name"] for s in c.get("/v1/sources", headers=A).json()] == ["bank_pg"]
    for call in [lambda: c.post("/v1/ask", json={"question": "x", "source": "financial"}, headers=A),
                 lambda: c.get("/v1/sources/financial/tables", headers=A),
                 lambda: c.post("/v1/sql/validate", json={"source": "financial", "sql": "SELECT 1"}, headers=A)]:
        assert call().status_code == 403
    assert c.get("/v1/sources/bank_pg/tables", headers=A).status_code == 200
    assert c.post("/v1/ask", json={"question": "x"}, headers=A).json()["source"] == "bank_pg"  # default = first allowed


def test_unknown_source_does_not_list_others(two_keys, make_agent, tmp_path, monkeypatch):
    monkeypatch.setenv("BANKQL_API_KEYS", "k")
    c = TestClient(create_app(make_agent(agent_fake({}))))
    r = c.get("/v1/sources/nope/tables", headers={"X-API-Key": "k"})
    assert r.status_code == 404 and "financial" not in r.text and "bank_pg" not in r.text


def test_export_bound_to_key_and_expires(two_keys):
    c, agent = two_keys
    r = c.post("/v1/ask", json={"question": "How many loans are in debt?"}, headers=A).json()
    assert r["status"] == "ok" and r["export_id"] and len(r["export_id"]) == 32
    url = f"/v1/results/{r['export_id']}.csv"
    assert c.get(url, headers=A).status_code == 200
    assert c.get(url, headers=B).status_code == 404  # another key: indistinguishable from "no such export"
    agent.exporter.ttl_s = 0.01
    time.sleep(0.05)
    assert c.get(url, headers=A).status_code == 404
    agent.exporter.purge()
    assert not list(agent.exporter.dir.glob(f"{r['export_id']}.*"))


def test_export_ids_are_random_128_bit(tmp_path, sqlite_src):
    from bankql.export import Exporter

    ids = {Exporter(tmp_path).write_rows(["a"], [[1]]).id for _ in range(50)}
    assert len(ids) == 50 and all(len(i) == 32 and int(i, 16) for i in ids)


def test_errors_carry_no_internals(two_keys):
    c, _ = two_keys
    r = c.post("/v1/ask", json={"question": "list zzq everything"}, headers=A).json()
    assert r["status"] == "refused"
    for needle in ("[SQL:", "Background on this error", "psycopg", "sqlalche.me", "Traceback", "password", "host="):
        assert needle not in (r["refusal_reason"] or "") and needle not in str(r["warnings"])


def test_internal_error_response_is_generic(make_agent, src):
    class Boom:
        def generate(self, *a, **k):
            raise RuntimeError("secret detail: postgresql://bankql_ro:hunter2@db/core")
    a = make_agent(agent_fake({"in debt": route("analytics")}))
    a.engine = Boom()
    r = a.ask("How many loans are in debt?", source=src.name)
    assert r.status == "error" and "hunter2" not in r.model_dump_json() and r.request_id in r.refusal_reason
    assert "hunter2" in str(a.audit.recent(1)[0]["warnings"])  # details kept for operators


def test_unhandled_exception_returns_generic_500(make_agent, monkeypatch):
    monkeypatch.setenv("BANKQL_API_KEYS", "k")
    agent = make_agent(agent_fake({}))
    src = agent.sources["financial"]
    monkeypatch.setattr(src, "describe_table", lambda t: (_ for _ in ()).throw(RuntimeError("internal path /etc/bankql")))
    c = TestClient(create_app(agent), raise_server_exceptions=False)
    r = c.get("/v1/sources/financial/tables", headers={"X-API-Key": "k"})
    assert r.status_code == 500 and r.json() == {"detail": "internal error"} and "/etc/bankql" not in r.text


def test_api_keys_compared_by_hash_not_stored(tmp_path, monkeypatch):
    monkeypatch.setenv("BANKQL_API_KEYS", "")
    (tmp_path / "api_keys.yaml").write_text(f"keys:\n  - {{id: x, sha256: {hash_key('s3cret')}}}\n")
    ks = KeyStore(tmp_path)
    assert ks.authenticate("s3cret").key_id == "x" and ks.authenticate("s3cre") is None and ks.authenticate("") is None
    assert "s3cret" not in (tmp_path / "api_keys.yaml").read_text()
