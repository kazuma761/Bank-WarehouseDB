"""Evaluation harness: resumable JSONL runner, BIRD-faithful metrics, SQLAS hooks, leakage checks."""
from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from pathlib import Path


def load_jsonl(path: str | Path) -> list[dict]:
    """Read JSONL, tolerating a torn last line from an interrupted run."""
    out = []
    p = Path(path)
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def run_jsonl(items: Iterable[dict], fn: Callable[[dict], dict], out_path: str | Path, *, key: str = "id",
              limit: int | None = None, retry_errors: bool = True) -> dict:
    """Run fn over items, appending one JSON line per item and skipping items already recorded.
    Interrupt any time; rerun to continue. `limit` processes at most N new items (chunked CPU runs)."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = {r[key]: r for r in load_jsonl(out)}
    done_ok = {k for k, r in done.items() if not (retry_errors and r.get("error"))}
    ran = skipped = errors = 0
    with out.open("a", encoding="utf-8") as fh:
        for item in items:
            if item[key] in done_ok:
                skipped += 1
                continue
            if limit is not None and ran >= limit:
                break
            try:
                rec = {key: item[key], **fn(item)}
            except Exception as e:  # noqa: BLE001 — record and continue; retried on the next run
                rec = {key: item[key], "error": f"{type(e).__name__}: {str(e)[:500]}"}
                errors += 1
            fh.write(json.dumps(rec, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
            ran += 1
    return {"ran": ran, "skipped": skipped, "errors": errors, "path": str(out)}


def latest(records: list[dict], key: str = "id") -> list[dict]:
    """Last record per item (a retried error is superseded by its rerun)."""
    by = {}
    for r in records:
        by[r[key]] = r
    return list(by.values())
