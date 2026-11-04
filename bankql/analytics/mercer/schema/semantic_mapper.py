"""SemanticMapper — merges user-defined descriptions into RawSchema.

Loads config/mappings.yaml and injects table descriptions, column
descriptions, and business glossary terms into an AnnotatedSchema.
Columns and tables with no mapping entry keep description=None.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from bankql.analytics.mercer.core.models import AnnotatedSchema, ColumnSchema, RawSchema, TableSchema


class SemanticMapper:
    def __init__(self, mappings_path: str = "config/mappings.yaml") -> None:
        self._path = Path(mappings_path)
        self._mappings: dict[str, Any] | None = None

    def annotate(self, raw: RawSchema) -> AnnotatedSchema:
        """Merge user-defined descriptions and glossary from mappings.yaml into *raw*."""
        mappings = self._load()
        tables_config: dict[str, Any] = mappings.get("tables", {})
        glossary: dict[str, str] = mappings.get("glossary", {})

        annotated_tables: list[TableSchema] = []
        for table in raw.tables:
            table_cfg = tables_config.get(table.name, {})
            col_cfg: dict[str, str] = table_cfg.get("columns", {})

            annotated_cols: list[ColumnSchema] = [
                col.model_copy(update={"description": col_cfg.get(col.name)})
                for col in table.columns
            ]
            annotated_tables.append(
                table.model_copy(update={
                    "description": table_cfg.get("description"),
                    "columns": annotated_cols,
                })
            )

        return AnnotatedSchema(
            db_url_hash=raw.db_url_hash,
            tables=annotated_tables,
            glossary=glossary,
        )

    # ------------------------------------------------------------------

    def _load(self) -> dict[str, Any]:
        if self._mappings is None:
            if not self._path.exists():
                self._mappings = {}
            else:
                with self._path.open(encoding="utf-8") as fh:
                    self._mappings = yaml.safe_load(fh) or {}
        return self._mappings
