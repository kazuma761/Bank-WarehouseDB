from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class ColumnSchema(BaseModel):
    name: str
    type: str
    description: str | None = None
    is_primary_key: bool = False
    is_foreign_key: bool = False
    references_table: str | None = None   # populated by introspector for FK columns
    references_column: str | None = None  # populated by introspector for FK columns
    # Up to N distinct values sampled from the column at introspection time.
    # Used by Stage 1 (LSH/value matching) and Stage 2 (M-Schema prompts).
    # None means sampling has not been attempted; an empty list means the
    # column has no usable values (BLOB, very long text, or empty).
    sample_values: list[str] | None = None


class TableSchema(BaseModel):
    name: str
    description: str | None = None
    columns: list[ColumnSchema]


class JoinPath(BaseModel):
    from_table: str
    from_column: str
    to_table: str
    to_column: str


class RawSchema(BaseModel):
    db_url_hash: str
    tables: list[TableSchema]


class AnnotatedSchema(RawSchema):
    glossary: dict[str, str] = {}


class EntityMatch(BaseModel):
    token: str
    table: str
    column: str
    matched_value: str
    score: float


class EntityContext(BaseModel):
    original_question: str
    expanded_question: str
    glossary_expansions: dict[str, str]
    entity_matches: list[EntityMatch]


class FilteredSchema(BaseModel):
    tables: list[TableSchema]
    join_paths: list[JoinPath]


class QueryPlan(BaseModel):
    aggregations: list[str]
    filters: list[str]
    joins: list[str]
    ordering: str | None = None
    subqueries: list[str]
    reasoning: str


class ExecutionResult(BaseModel):
    success: bool
    sql: str
    row_count: int
    columns: list[str]
    sample_rows: list[dict[str, Any]]
    error_message: str | None = None
    execution_time_ms: float = 0.0


class SQLCandidate(BaseModel):
    sql: str
    strategy: str
    execution_result: ExecutionResult | None = None
    score: float = 0.0


class CorrectionStep(BaseModel):
    iteration: int
    error_class: str
    original_sql: str
    corrected_sql: str
    success: bool


class AuditEntry(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    timestamp: datetime
    question: str
    final_sql: str | None = None
    tables_used: list[str]
    columns_used: list[str]
    model_used: str
    backend_used: str
    correction_steps: list[CorrectionStep]
    total_latency_ms: float
    success: bool
    stage_timings: dict[str, float] = Field(default_factory=dict)


class PipelineState(BaseModel):
    question: str
    entity_context: EntityContext | None = None
    filtered_schema: FilteredSchema | None = None
    query_plan: QueryPlan | None = None
    candidates: list[SQLCandidate] = Field(default_factory=list)
    best_candidate: SQLCandidate | None = None
    final_sql: str | None = None
    correction_log: list[CorrectionStep] = Field(default_factory=list)
    audit_entry: AuditEntry | None = None
    stage_timings: dict[str, float] = Field(default_factory=dict)
