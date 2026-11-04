"""Audit log: one row per question in a local DuckDB table (question, route, SQL, row count, latency, refusal)."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import duckdb

_DDL = """CREATE TABLE IF NOT EXISTS audit_log (
    ts TIMESTAMP, request_id VARCHAR, session_id VARCHAR, surface VARCHAR, source VARCHAR,
    question VARCHAR, standalone_question VARCHAR, route VARCHAR, intent VARCHAR, sql VARCHAR,
    row_count BIGINT, latency_ms DOUBLE, refused BOOLEAN, refusal_reason VARCHAR, warnings VARCHAR)"""
COLUMNS = ["ts", "request_id", "session_id", "surface", "source", "question", "standalone_question", "route",
           "intent", "sql", "row_count", "latency_ms", "refused", "refusal_reason", "warnings"]


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._con = duckdb.connect(str(self.path))
        self._con.execute(_DDL)

    def write(self, **row) -> None:
        row = {c: row.get(c) for c in COLUMNS}
        row["ts"] = row["ts"] or datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC (TIMESTAMPTZ needs pytz)
        row["warnings"] = json.dumps(row["warnings"] or [])
        with self._lock:
            self._con.execute(f"INSERT INTO audit_log VALUES ({', '.join('?' * len(COLUMNS))})",
                              [row[c] for c in COLUMNS])

    def recent(self, n: int = 20) -> list[dict]:
        with self._lock:
            cur = self._con.execute("SELECT * FROM audit_log ORDER BY ts DESC LIMIT ?", [n])
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def close(self) -> None:
        with self._lock:
            self._con.close()
