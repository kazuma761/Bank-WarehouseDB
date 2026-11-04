from bankql.audit import AuditLog


def test_audit_roundtrip(tmp_path):
    log = AuditLog(tmp_path / "audit.duckdb")
    log.write(question="how many loans?", route="analytics", sql="SELECT 1", row_count=1, latency_ms=12.5,
              refused=False, warnings=["sqlas_input: DANGEROUS_INPUT: bulk_pii_request"])
    log.write(question="drop all tables", route="refused", refused=True, refusal_reason="Input blocked")
    rows = log.recent()
    assert len(rows) == 2 and {r["route"] for r in rows} == {"analytics", "refused"}
    assert "bulk_pii_request" in next(r for r in rows if r["route"] == "analytics")["warnings"]
