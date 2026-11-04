"""MercerPipeline — Phase 1 orchestrator.

(bankql port: see bankql/analytics/mercer/__init__.py for the patch list.)

Active stages:
  Stage 1 — EntityRetriever  (BM25 + LSH + glossary)
  Stage 2 — CHESSSchemaLinker (column pre-filter → LLM table selection → LLM column selection)
  Stage 3 — QueryDecomposer  (structured SQL planning)
  Stage 4 — CandidateGenerator (3 strategies, dispatched in parallel)
  Stage 5 — CandidateExecutor  (parallel execution + scoring + selection)

Schema is loaded from Redis cache on first call; cache miss triggers
introspection + annotation + cache write.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncEngine

from bankql.analytics.mercer.core.candidate_generator import CandidateGenerator
from bankql.analytics.mercer.core.corrector import TaxonomyCorrector
from bankql.analytics.mercer.core.entity_retriever import EntityRetriever
from bankql.analytics.mercer.core.executor import CandidateExecutor
from bankql.analytics.mercer.core.logging import get_logger
from bankql.analytics.mercer.core.models import AnnotatedSchema, AuditEntry, CorrectionStep, PipelineState, SQLCandidate
from bankql.analytics.mercer.core.query_decomposer import QueryDecomposer
from bankql.analytics.mercer.core.schema_linker import CHESSSchemaLinker
from bankql.analytics.mercer.db.sandbox import ReadOnlySandbox
from bankql.analytics.mercer.inference.base import LLMBackend
from bankql.analytics.mercer.schema.cache import SchemaCache
from bankql.analytics.mercer.schema.graph_builder import FKGraphBuilder
from bankql.analytics.mercer.schema.introspector import SchemaIntrospector
from bankql.analytics.mercer.schema.semantic_mapper import SemanticMapper

logger = get_logger(__name__)

_DIRECT_COT = "direct_cot"


def _pick_winner(scored: list[SQLCandidate]) -> SQLCandidate | None:
    """Return the best scored candidate, or None if the list is empty."""
    if not scored:
        return None
    successful = [c for c in scored if c.execution_result and c.execution_result.success]
    if successful:
        return max(successful, key=lambda c: (c.score, c.strategy == _DIRECT_COT))
    # All failed — prefer direct_cot for downstream correction
    for c in scored:
        if c.strategy == _DIRECT_COT:
            return c
    return scored[0]


class MercerPipeline:
    """End-to-end Text-to-SQL pipeline.

    Args:
        db_engine:     SQLAlchemy async engine for the target database.
        cfg:           Settings instance. Defaults to the global singleton.
        backend:       Optional pre-built LLMBackend. Bypasses the router —
                       useful for tests and for injecting specific models.
        mappings_path: Path to mappings.yaml for semantic annotation.
        audit_path:    Path to the append-only audit log (JSONL).
        redis_url:     Redis URL for schema cache.
        cache_ttl:     Schema cache TTL in seconds (default: 3600).
        cache:         Pre-built SchemaCache. Overrides redis_url/cache_ttl when set
                       (useful for injecting a fakeredis-backed cache in tests).
    """

    def __init__(
        self,
        db_engine: AsyncEngine,
        *,
        backend: LLMBackend,  # bankql patch: required (ModelRouter / cloud backends removed)
        mappings_path: str = "config/mappings.yaml",
        audit_path: str = "logs/audit.duckdb",
        redis_url: str = "redis://localhost:6379",
        cache_ttl: int = 3600,
        cache: SchemaCache | None = None,
        audit_store: object | None = None,  # bankql patch: any object with `async append(entry)`
        annotated_schema: AnnotatedSchema | None = None,  # bankql patch: prebuilt (allowlisted) schema
        sandbox: ReadOnlySandbox | None = None,  # bankql patch: injected sandbox (our validator first)
    ) -> None:
        self._engine = db_engine
        self._backend = backend
        self._audit_store = audit_store

        self._sandbox = sandbox or ReadOnlySandbox()
        self._introspector = SchemaIntrospector()
        self._mapper = SemanticMapper(mappings_path=mappings_path)
        self._cache = cache if cache is not None else SchemaCache(
            redis_url=redis_url, ttl_seconds=cache_ttl
        )

        # Populated lazily on first call to _load_schema()
        self._annotated_schema: AnnotatedSchema | None = annotated_schema
        # Populated lazily once schema is loaded
        self._entity_retriever: EntityRetriever | None = None
        self._schema_linker: CHESSSchemaLinker | None = None

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    async def run(self, question: str) -> PipelineState:
        """Run the full pipeline and return the final PipelineState."""
        wall_start = time.monotonic()
        state = PipelineState(question=question)

        # Schema — lazy, cache-first (Redis → introspect)
        schema = await self._load_schema()

        # Backend selection
        backend = self._backend
        model_used = getattr(backend, "_model", "unknown")
        backend_used = type(backend).__name__

        logger.debug("pipeline_start", question=question, backend=backend_used, model=model_used)

        stage_timings: dict[str, float] = {}

        # Stage 1 — entity retrieval
        _t = time.perf_counter()
        entity_retriever = self._get_entity_retriever(schema)
        entity_context = entity_retriever.retrieve(question)
        stage_timings["entity_retrieval"] = round((time.perf_counter() - _t) * 1000, 3)
        state = state.model_copy(update={"entity_context": entity_context})
        logger.debug(
            "stage1_complete",
            entity_matches=len(entity_context.entity_matches),
            glossary_expansions=len(entity_context.glossary_expansions),
        )

        # Stage 2 — CHESS schema linking
        _t = time.perf_counter()
        schema_linker = self._get_schema_linker(backend)
        filtered_schema = await schema_linker.link(question, entity_context, schema)
        stage_timings["schema_linking"] = round((time.perf_counter() - _t) * 1000, 3)
        state = state.model_copy(update={"filtered_schema": filtered_schema})
        logger.debug(
            "stage2_complete",
            tables=[t.name for t in filtered_schema.tables],
            join_paths=len(filtered_schema.join_paths),
        )

        # Stage 3 — query decomposition
        _t = time.perf_counter()
        decomposer = QueryDecomposer(backend)
        query_plan = await decomposer.decompose(question, filtered_schema)
        stage_timings["query_decomposition"] = round((time.perf_counter() - _t) * 1000, 3)
        state = state.model_copy(update={"query_plan": query_plan})
        logger.debug(
            "stage3_complete",
            aggregations=len(query_plan.aggregations),
            joins=len(query_plan.joins),
        )

        # Stage 4 — multi-candidate SQL generation
        # We pass the expanded question (question + glossary parentheticals)
        # in addition to the original. This is the E-SQL pattern: rewriting
        # the question to inline schema-linked terms reliably improves
        # generation accuracy on hard queries with little cost.
        _t = time.perf_counter()
        generator = CandidateGenerator(backend)
        candidates = await generator.generate_candidates(
            question=question,
            expanded_question=entity_context.expanded_question,
            filtered_schema=filtered_schema,
            query_plan=query_plan,
        )
        stage_timings["candidate_generation"] = round((time.perf_counter() - _t) * 1000, 3)
        logger.debug("stage4_complete", candidates=len(candidates))

        # Stage 5 — parallel execution, scoring, selection
        _t = time.perf_counter()
        executor = CandidateExecutor(self._sandbox, self._engine)
        scored_candidates = await executor.execute_and_score(candidates)
        best = _pick_winner(scored_candidates)
        stage_timings["execution_scoring"] = round((time.perf_counter() - _t) * 1000, 3)

        exec_success = (
            best is not None
            and best.execution_result is not None
            and best.execution_result.success
        )
        final_sql = best.sql if (best is not None and exec_success) else None

        state = state.model_copy(update={
            "candidates": scored_candidates,
            "best_candidate": best,
            "final_sql": final_sql,
        })

        # Stage 6 — taxonomy-guided correction (only when best candidate failed)
        correction_log: list[CorrectionStep] = []
        if not exec_success and best is not None:
            _t = time.perf_counter()
            corrector = TaxonomyCorrector(backend, self._sandbox, self._engine)
            corrected_sql, correction_log = await corrector.correct(
                failed_candidate=best,
                question=question,
                filtered_schema=filtered_schema,
            )
            stage_timings["correction"] = round((time.perf_counter() - _t) * 1000, 3)
            if corrected_sql is not None:
                final_sql = corrected_sql
                exec_success = True
            state = state.model_copy(update={
                "final_sql": final_sql,
                "correction_log": correction_log,
            })

        state = state.model_copy(update={"stage_timings": stage_timings})

        # Audit
        elapsed_ms = round((time.monotonic() - wall_start) * 1000, 2)
        tables_used = [t.name for t in filtered_schema.tables]
        columns_used = [
            f"{t.name}.{c.name}"
            for t in filtered_schema.tables
            for c in t.columns
        ]
        audit_entry = AuditEntry(
            timestamp=datetime.now(UTC),
            question=question,
            final_sql=final_sql,
            tables_used=tables_used,
            columns_used=columns_used,
            model_used=model_used,
            backend_used=backend_used,
            correction_steps=correction_log,
            total_latency_ms=elapsed_ms,
            success=exec_success,
            stage_timings=stage_timings,
        )
        state = state.model_copy(update={"audit_entry": audit_entry})
        await self._write_audit(audit_entry)

        logger.info(
            "pipeline_complete",
            question=question,
            success=exec_success,
            latency_ms=elapsed_ms,
        )
        return state

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _load_schema(self) -> AnnotatedSchema:
        """Return the AnnotatedSchema, using in-memory → Redis → introspect order."""
        if self._annotated_schema is not None:
            return self._annotated_schema

        # Introspect first to get the hash, then check cache
        raw = await self._introspector.introspect(self._engine)
        cached = await self._cache.get(raw.db_url_hash)
        if cached is not None:
            logger.debug("schema_from_cache", db_url_hash=raw.db_url_hash)
            self._annotated_schema = cached
            return cached

        # Cache miss — annotate and store
        annotated = self._mapper.annotate(raw)
        await self._cache.set(annotated.db_url_hash, annotated)
        logger.debug(
            "schema_introspected_and_cached",
            tables=len(annotated.tables),
            db_url_hash=annotated.db_url_hash,
        )
        self._annotated_schema = annotated
        return annotated

    def _get_entity_retriever(self, schema: AnnotatedSchema) -> EntityRetriever:
        if self._entity_retriever is None:
            self._entity_retriever = EntityRetriever(
                schema=schema, glossary=schema.glossary
            )
        return self._entity_retriever

    def _get_schema_linker(self, backend: LLMBackend) -> CHESSSchemaLinker:
        if self._schema_linker is None:
            assert self._annotated_schema is not None
            fk_graph = FKGraphBuilder(self._annotated_schema)
            self._schema_linker = CHESSSchemaLinker(
                llm_backend=backend, fk_graph=fk_graph
            )
        return self._schema_linker

    async def _write_audit(self, entry: AuditEntry) -> None:
        if self._audit_store is not None:
            await self._audit_store.append(entry)
