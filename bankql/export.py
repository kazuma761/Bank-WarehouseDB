"""Stream full results to CSV (or Parquet via DuckDB's built-in writer) — big results never go to the LLM or
the chat; the API serves the file by an unguessable export id."""
from __future__ import annotations

import csv
import re
import uuid
from dataclasses import dataclass
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


def _csv_cell(v):
    v = jsonable(v)
    # Spreadsheet formula injection: neutralise text cells that a spreadsheet would evaluate.
    if isinstance(v, str) and v.startswith(_FORMULA_START):
        return "'" + v
    return v


class Exporter:
    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def export(self, source: Source, sql: str, params: dict | None = None, *, fmt: str = "csv",
               allow_pii_output: bool = False) -> Export:
        if fmt not in ("csv", "parquet"):
            raise ValueError("fmt must be csv or parquet")
        eid = uuid.uuid4().hex
        path = self.dir / f"{eid}.csv"
        n = 0
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            header = False
            for cols, rows in source.stream(sql, params, allow_pii_output=allow_pii_output):
                if not header:
                    w.writerow(cols)
                    header = True
                w.writerows([_csv_cell(v) for v in r] for r in rows)
                n += len(rows)
        if fmt == "parquet":
            import duckdb

            pq = self.dir / f"{eid}.parquet"
            # shortcut: types are re-inferred from the CSV; write Parquet directly from the source if exact types matter.
            duckdb.connect().execute(f"COPY (SELECT * FROM read_csv('{path.as_posix()}', header=true)) TO '{pq.as_posix()}' (FORMAT parquet)")
            path.unlink()
            path = pq
        return Export(eid, path, n, n >= source.cfg.row_cap)

    def path_for(self, export_id: str, fmt: str = "csv") -> Path | None:
        if not _ID.match(export_id) or fmt not in ("csv", "parquet"):
            return None
        p = self.dir / f"{export_id}.{fmt}"
        return p if p.exists() else None
