"""D2: small-count rule against letter-by-letter name extraction, and per-key lookup rate limits."""
import string

import pytest
from fastapi.testclient import TestClient

from bankql.api import create_app
from bankql.auth import KeyStore, hash_key
from bankql.connectors import QueryRejected
from tests.fakes import agent_fake, route


def test_letter_by_letter_extraction_learns_nothing(src):
    """Probe client 12's name one letter at a time: every answer is the same '<5', so no letter is revealed."""
    answers = {ch: src.run(f"SELECT COUNT(*) AS n FROM client_profile WHERE client_id = 12 AND full_name LIKE '{ch}%'").rows[0][0]
               for ch in string.ascii_uppercase}
    assert set(answers.values()) == {"<5"}
    first = src.run("SELECT full_name FROM client_profile WHERE client_id = 12", allow_pii_output=True).rows[0][0][0]
    assert answers[first.upper()] == "<5"  # the true letter looks exactly like the others


@pytest.mark.parametrize("probe", [
    "SELECT COUNT(*) FROM client_profile WHERE client_id <= 5 OR (client_id = 12 AND full_name LIKE 'A%')",
    "SELECT client_id FROM client_profile WHERE client_id = 12 AND full_name LIKE 'A%'",
    "SELECT CASE WHEN full_name LIKE 'A%' THEN 1 ELSE 0 END FROM client_profile WHERE client_id = 12",
    "SELECT MIN(client_id) FROM client_profile WHERE full_name LIKE 'A%'",
    "SELECT COUNT(*) FROM client_profile WHERE NOT (full_name LIKE 'A%') AND client_id = 12",
    "SELECT COUNT(*) FROM client_profile WHERE client_id = 12 AND SUBSTR(full_name, 1, 1) = 'A' UNION SELECT 1",
])
def test_probe_shapes_are_refused(src, probe):
    with pytest.raises(QueryRejected) as e:
        src.run(probe)
    assert e.value.validation.code in ("pii_filter_shape", "pii_output")


def test_exports_are_bucketed_too(sqlite_src, tmp_path):
    from bankql.export import Exporter

    e = Exporter(tmp_path).export(sqlite_src, "SELECT COUNT(*) AS n FROM client_profile WHERE full_name = 'Priya Patel'")
    assert e.path.read_text().splitlines() == ["n", "<5"]


def test_lookup_rate_limit_per_key(make_agent, tmp_path, monkeypatch):
    monkeypatch.setenv("BANKQL_API_KEYS", "")
    (tmp_path / "api_keys.yaml").write_text(
        f"keys:\n  - {{id: a, sha256: {hash_key('key-a')}, lookups_per_minute: 3}}\n  - {{id: b, sha256: {hash_key('key-b')}}}\n")
    agent = make_agent(agent_fake({"client 12": route(tools=[{"tool": "get_client", "client_id": 12}])}))
    c = TestClient(create_app(agent, keys=KeyStore(tmp_path)))
    codes = [c.post("/v1/ask", json={"question": "client 12 details"}, headers={"X-API-Key": "key-a"}).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    r = c.post("/v1/ask", json={"question": "client 12 details"}, headers={"X-API-Key": "key-a"})
    assert int(r.headers["retry-after"]) >= 1
    assert c.post("/v1/ask", json={"question": "client 12 details"}, headers={"X-API-Key": "key-b"}).status_code == 200  # per key
    assert any(row["route"] == "refused" and "rate limit" in (row["refusal_reason"] or "") for row in agent.audit.recent(10))
