"""Semantic model: one YAML per source (or several merged), the single source of truth for descriptions,
codes, PII flags and time columns."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class Column(BaseModel):
    description: str = ""
    valid_values: dict[str, str] = Field(default_factory=dict)  # code -> English meaning
    aliases: list[str] = Field(default_factory=list)  # synonyms for the column itself
    value_aliases: dict[str, str] = Field(default_factory=dict)  # phrase -> SQL condition
    pii: bool = False


class Table(BaseModel):
    description: str = ""
    columns: dict[str, Column] = Field(default_factory=dict)
    time_column: str | None = None  # large-table flags live per source in sources.yaml
    optional: bool = False  # may be absent from a source (e.g. synthetic client_profile)

    def column(self, name: str) -> Column | None:
        """Case-insensitive: Postgres folds unquoted A2 to a2."""
        return self.columns.get(name) or next((c for k, c in self.columns.items() if k.lower() == name.lower()), None)


class Metric(BaseModel):
    sql: str
    description: str = ""


class SemanticModel(BaseModel):
    name: str
    description: str = ""
    tables: dict[str, Table] = Field(default_factory=dict)
    joins: list[list[str]] = Field(default_factory=list)
    glossary: dict[str, str] = Field(default_factory=dict)
    metrics: dict[str, Metric] = Field(default_factory=dict)

    def table(self, name: str) -> Table | None:
        return self.tables.get(name) or next((t for k, t in self.tables.items() if k.lower() == name.lower()), None)

    def pii_columns(self) -> set[tuple[str, str]]:
        return {(t.lower(), c.lower()) for t, tab in self.tables.items() for c, col in tab.columns.items() if col.pii}

    def legend(self, columns: list[str], tables: list[str] | None = None) -> dict[str, dict[str, str]]:
        """Code meanings for result columns that hold coded values (e.g. PRIJEM -> credit), looked up only in the
        *tables* the query read (trans.type, disp.type and card.type are different codes)."""
        out: dict[str, dict[str, str]] = {}
        names = {t.split(".")[-1].lower() for t in tables} if tables is not None else None
        for col in columns:
            for tname, tab in self.tables.items():
                if names is not None and tname.lower() not in names:
                    continue
                c = tab.column(col)
                if c and c.valid_values:
                    out.setdefault(col, {}).update(c.valid_values)
        return out


def load_semantic(paths: list[str | Path]) -> SemanticModel:
    """Load and merge semantic YAML files; later files add/override tables and glossary entries."""
    merged: dict = {}
    for p in paths:
        doc = yaml.safe_load(Path(p).read_text(encoding="utf-8")) or {}
        if not merged:
            merged = doc
            continue
        for key in ("tables", "glossary", "metrics"):
            merged.setdefault(key, {}).update(doc.get(key, {}))
        merged.setdefault("joins", []).extend(doc.get("joins", []))
    return SemanticModel.model_validate(merged)
