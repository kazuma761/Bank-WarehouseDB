"""Stream full results to CSV (or Parquet via DuckDB's built-in writer) — big results never go to the LLM or
the chat; the API serves the file by an unguessable export id."""
from __future__ import annotations

import csv
import re
import uuid
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
    def __init__(self, directory: str | Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def export(self, source: Source, sql: str, params: dict | None = None, *, fmt: str = "csv",
               allow_pii_output: bool = False, preview_rows: int = 100) -> Export:
        if fmt not in ("csv", "parquet"):
            raise ValueError("fmt must be csv or parquet")
        eid = uuid.uuid4().hex
        path = self.dir / f"{eid}.csv"
        n, columns, preview = 0, [], []
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            for cols, rows in source.stream(sql, params, allow_pii_output=allow_pii_output):
                if not columns:
                    columns = cols
                    w.writerow(cols)
                if len(preview) < preview_rows:
                    preview += rows[: preview_rows - len(preview)]
                w.writerows([_csv_cell(v) for v in r] for r in rows)
                n += len(rows)
            if not columns:  # empty result: still write the header
                columns = source.run(sql, params, max_rows=1, allow_pii_output=allow_pii_output).columns
                w.writerow(columns)
        if fmt == "parquet":
            import duckdb

            pq = self.dir / f"{eid}.parquet"
            # shortcut: types are re-inferred from the CSV; write Parquet directly from the source if exact types matter.
            duckdb.connect().execute(f"COPY (SELECT * FROM read_csv('{path.as_posix()}', header=true)) TO '{pq.as_posix()}' (FORMAT parquet)")
            path.unlink()
            path = pq
        return Export(eid, path, n, n >= source.cfg.row_cap, columns, preview)

    def write_rows(self, columns: list[str], rows: list[list]) -> Export:
        """Export rows already in hand (lookup results) without re-running the query."""
        eid = uuid.uuid4().hex
        path = self.dir / f"{eid}.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(columns)
            w.writerows([_csv_cell(v) for v in r] for r in rows)
        return Export(eid, path, len(rows), False, columns, rows[:100])

    def path_for(self, export_id: str, fmt: str = "csv") -> Path | None:
        if not _ID.match(export_id) or fmt not in ("csv", "parquet"):
            return None
        p = self.dir / f"{export_id}.{fmt}"
        return p if p.exists() else None
