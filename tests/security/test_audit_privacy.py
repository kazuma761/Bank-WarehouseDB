"""D4: audit log holds no raw PII values, is owner-only, and honours retention."""
import os
import stat
from datetime import datetime, timedelta, timezone

from bankql.audit import AuditLog
from tests.fakes import agent_fake, route


def test_audit_masks_names_phones_and_pii_literals(make_agent, src):
    a = make_agent(agent_fake({"rahul": route(tools=[{"tool": "get_client", "client_name": "Rahul Sharma"}]),
                               "client 1 ": route(tools=[{"tool": "get_client", "client_id": 1}]),
                               "named": route("analytics")}, summary="ok"),
                   {"named": "SELECT COUNT(*) AS n FROM client_profile WHERE full_name LIKE 'Ananya%'"})
    a.ask("rahul sharma ka profile", source=src.name)                  # clarification lists two names
    a.ask("profile of client 1 please, call +91 98450 12345", source=src.name)
    from bankql.auth import Policy, Principal

    a.ask("how many clients are named Ananya?", source=src.name, principal=Principal("priv", Policy(pii_filter=True)))
    blob = str(a.audit.recent(10))
    for secret in ("Rahul Sharma", "rahul sharma", "98450 12345", "'Ananya%'"):
        assert secret not in blob, secret
    assert "[PII]" in blob and "'***'" in blob  # masked, not dropped


def test_audit_file_is_owner_only_and_retention_purges(tmp_path):
    log = AuditLog(tmp_path / "a.duckdb", retention_days=30)
    assert stat.S_IMODE(os.stat(tmp_path / "a.duckdb").st_mode) == 0o600
    log.write(question="old", ts=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=31))
    log.write(question="new")
    log.purge()
    assert [r["question"] for r in log.recent()] == ["new"]
