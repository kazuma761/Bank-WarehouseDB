"""Generate Mercer's mappings.yaml from our semantic model, so descriptions never drift apart.

Mercer's format (third_party/mercer/config/mappings.yaml @ d6f922f):
    tables: {<table>: {description: str, columns: {<column>: str}}}
    glossary: {<term>: str}
Coded values and aliases are folded into the column description text, and value aliases become glossary
entries, because Mercer's SemanticMapper only reads plain strings.

    .venv/bin/python -m bankql.semantic.mercer_mappings config/semantic/financial.yaml -o data/mercer_mappings.yaml
"""
from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from bankql.semantic import Column, SemanticModel, load_semantic


def column_text(col: Column) -> str:
    parts = [col.description]
    if col.valid_values:
        parts.append("Values: " + "; ".join(f"'{k}' = {v}" for k, v in col.valid_values.items()))
    if col.aliases:
        parts.append("Also called: " + ", ".join(col.aliases))
    return ". ".join(p for p in parts if p)


def to_mercer(model: SemanticModel, db_names: dict[str, list[str]] | None = None) -> dict:
    """*db_names* = {table: [columns]} as the target database reports them, so keys match its case
    (Postgres folds unquoted A2 to a2, and Mercer's SemanticMapper matches keys exactly)."""
    tables = {}
    glossary = dict(model.glossary)
    real = {t.lower(): (t, {c.lower(): c for c in cols}) for t, cols in (db_names or {}).items()}
    for tname, tab in model.tables.items():
        desc = tab.description
        if tab.time_column:
            desc += f" Time column: {tab.time_column}."
        rt, rcols = real.get(tname.lower(), (tname, {}))
        tables[rt] = {"description": desc.strip(),
                      "columns": {rcols.get(c.lower(), c): column_text(col) for c, col in tab.columns.items()}}
        for cname, col in tab.columns.items():
            for phrase, cond in col.value_aliases.items():
                glossary.setdefault(phrase, f"{tname}.{cond}" if not cond.startswith(f"{tname}.") else cond)
    for name, m in model.metrics.items():
        glossary.setdefault(name.replace("_", " "), f"{m.sql} -- {m.description}")
    return {"tables": tables, "glossary": glossary}


def write_mappings(model: SemanticModel, out: str | Path, db_names: dict[str, list[str]] | None = None) -> Path:
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    header = f"# GENERATED from the semantic model '{model.name}' by bankql.semantic.mercer_mappings — do not edit.\n"
    out.write_text(header + yaml.safe_dump(to_mercer(model, db_names), sort_keys=False, allow_unicode=True), encoding="utf-8")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("semantic", nargs="+")
    ap.add_argument("-o", "--out", default="data/mercer_mappings.yaml")
    a = ap.parse_args()
    print(write_mappings(load_semantic(a.semantic), a.out))
