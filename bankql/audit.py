"""Audit log: one row per question in a local DuckDB table (question, route, SQL, row count, latency, refusal).
PII is masked before rows get here (bankql.privacy). The file is 0600 in a 0700 directory; rows older than
`retention_days` are deleted at start-up and every 1000 writes."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb

_DDL = """CREATE TABLE IF NOT EXISTS audit_log (
    ts TIMESTAMP, request_id VARCHAR, session_id VARCHAR, surface VARCHAR, source VARCHAR,
    question VARCHAR, standalone_question VARCHAR, route VARCHAR, intent VARCHAR, sql VARCHAR,
    row_count BIGINT, latency_ms DOUBLE, refused BOOLEAN, refusal_reason VARCHAR, warnings VARCHAR, user_id VARCHAR)"""
COLUMNS = ["ts", "request_id", "session_id", "surface", "source", "question", "standalone_question", "route",
           "intent", "sql", "row_count", "latency_ms", "refused", "refusal_reason", "warnings", "user_id"]


class AuditLog:
    def __init__(self, path: str | Path, retention_days: int = 90):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self._writes = 0
        self._con = duckdb.connect(str(self.path))
        self._con.execute(_DDL)
        self._con.execute("ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS user_id VARCHAR")  # older audit files
        os.chmod(self.path, 0o600)
        self.purge()

    def purge(self) -> None:
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=self.retention_days)
        with self._lock:
            self._con.execute("DELETE FROM audit_log WHERE ts < ?", [cutoff])

    def write(self, **row) -> None:
        row = {c: row.get(c) for c in COLUMNS}
        row["ts"] = row["ts"] or datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC (TIMESTAMPTZ needs pytz)
        row["warnings"] = json.dumps(row["warnings"] or [])
        with self._lock:
            self._con.execute(f"INSERT INTO audit_log ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})",
                              [row[c] for c in COLUMNS])
            self._writes += 1
        if self._writes % 1000 == 0:
            self.purge()

    def recent(self, n: int = 20) -> list[dict]:
        with self._lock:
            cur = self._con.execute("SELECT * FROM audit_log ORDER BY ts DESC LIMIT ?", [n])
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def close(self) -> None:
        with self._lock:
            self._con.close()
