"""Stream full results to CSV (or Parquet via DuckDB's built-in writer) — big results never go to the LLM or
the chat. Each export has a random 128-bit id, is bound to the API key that created it, and expires."""
from __future__ import annotations

import csv
import json
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from bankql.connectors import Source, jsonable

_ID = re.compile(r"^[0-9a-f]{32}$")
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


@dataclass
class Export:
    id: str
    path: Path
    rows: int
    truncated: bool  # hit the source row cap
    columns: list[str] = field(default_factory=list)
    preview: list[list] = field(default_factory=list)  # first `preview_rows` rows, raw values


def _csv_cell(v):
    v = jsonable(v)
    # Spreadsheet formula injection: neutralise text cells that a spreadsheet would evaluate.
    if isinstance(v, str) and v.startswith(_FORMULA_START):
        return "'" + v
    return v


class Exporter:
    def __init__(self, directory: str | Path, ttl_hours: float = 24):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self.ttl_s = ttl_hours * 3600

    def _new(self, owner: str) -> str:
        eid = secrets.token_hex(16)
        meta = self.dir / f"{eid}.meta.json"
        meta.write_text(json.dumps({"owner": owner, "created": time.time()}))
        os.chmod(meta, 0o600)
        self.purge()
        return eid

    def purge(self) -> int:
        """Delete expired exports (and their metadata). Called on every new export."""
        n = 0
        for meta in self.dir.glob("*.meta.json"):
            try:
                expired = time.time() - json.loads(meta.read_text())["created"] > self.ttl_s
            except (ValueError, KeyError, OSError):
                expired = True
            if expired:
                eid = meta.name.split(".")[0]
                for f in self.dir.glob(f"{eid}.*"):
                    f.unlink(missing_ok=True)
                n += 1
        return n

    def export(self, source: Source, sql: str, params: dict | None = None, *, fmt: str = "csv",
               allow_pii_output: bool = False, preview_rows: int = 100, owner: str = "library",
               allow_pii_filter: bool = False) -> Export:
        if fmt not in ("csv", "parquet"):
            raise ValueError("fmt must be csv or parquet")
        eid = self._new(owner)
        path = self.dir / f"{eid}.csv"
        n, columns, preview = 0, [], []
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            for cols, rows in source.stream(sql, params, allow_pii_output=allow_pii_output, allow_pii_filter=allow_pii_filter):
                if not columns:
                    columns = cols
                    w.writerow(cols)
                if len(preview) < preview_rows:
                    preview += rows[: preview_rows - len(preview)]
                w.writerows([_csv_cell(v) for v in r] for r in rows)
                n += len(rows)
            if not columns:  # empty result: still write the header
                columns = source.run(sql, params, max_rows=1, allow_pii_output=allow_pii_output,
                                     allow_pii_filter=allow_pii_filter).columns
                w.writerow(columns)
        if fmt == "parquet":
            import duckdb

            pq = self.dir / f"{eid}.parquet"
            # shortcut: types are re-inferred from the CSV; write Parquet directly from the source if exact types matter.
            duckdb.connect().execute(f"COPY (SELECT * FROM read_csv('{path.as_posix()}', header=true)) TO '{pq.as_posix()}' (FORMAT parquet)")
            path.unlink()
            path = pq
        return Export(eid, path, n, n >= source.cfg.row_cap, columns, preview)

    def write_rows(self, columns: list[str], rows: list[list], owner: str = "library") -> Export:
        """Export rows already in hand (lookup results) without re-running the query."""
        eid = self._new(owner)
        path = self.dir / f"{eid}.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(columns)
            w.writerows([_csv_cell(v) for v in r] for r in rows)
        return Export(eid, path, len(rows), False, columns, rows[:100])

    def path_for(self, export_id: str, fmt: str = "csv", owner: str = "library") -> Path | None:
        """The export file, only for its owner and only before it expires; otherwise None (→ 404, no hint why)."""
        if not _ID.match(export_id) or fmt not in ("csv", "parquet"):
            return None
        p, meta = self.dir / f"{export_id}.{fmt}", self.dir / f"{export_id}.meta.json"
        try:
            m = json.loads(meta.read_text())
        except (OSError, ValueError):
            return None
        if not secrets.compare_digest(str(m.get("owner")), owner) or time.time() - m.get("created", 0) > self.ttl_s:
            return None
        return p if p.exists() else None
