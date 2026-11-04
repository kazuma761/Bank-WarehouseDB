"""BIRD mini-dev loader. Inspects the real folder layout instead of assuming it: finds mini_dev_sqlite.json and
dev_databases/financial/financial.sqlite anywhere under the unzipped directory, filters db_id == "financial",
and verifies every gold SQL executes before it is used."""
from __future__ import annotations

import json
from pathlib import Path

from bankql.eval.metrics import sqlite_fetch


def find(root: str | Path, name: str) -> Path:
    hits = sorted(Path(root).rglob(name))
    if not hits:
        raise FileNotFoundError(f"{name} not found under {root}")
    return hits[0]


def load_bird(root: str | Path, db_id: str = "financial") -> tuple[list[dict], Path, list[dict]]:
    """Returns (items, db_path, broken). items: {id, question, evidence, gold_sql, difficulty}."""
    qfile = find(root, "mini_dev_sqlite.json")
    db = find(root, f"{db_id}.sqlite")
    raw = [q for q in json.loads(qfile.read_text(encoding="utf-8")) if q.get("db_id") == db_id]
    items, broken = [], []
    for q in raw:
        item = {"id": q.get("question_id", len(items)), "question": q["question"], "evidence": q.get("evidence", ""),
                "gold_sql": q["SQL"], "difficulty": q.get("difficulty", "")}
        try:
            sqlite_fetch(str(db), item["gold_sql"])
            items.append(item)
        except Exception as e:  # noqa: BLE001
            broken.append({**item, "error": str(e)[:300]})
    return items, db, broken
