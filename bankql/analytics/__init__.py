"""Analytics engine: natural language -> one validated SELECT. `AnalyticsEngine` is the swap-out interface;
`MercerEngine` adapts the ported Mercer pipeline (bankql/analytics/mercer, MIT).

fast mode — Mercer stages 1 (entity retrieval), 2 (schema linking), 4 (direct_cot only, T=0), 5 (EXPLAIN-gated
            execution) and 6 (correction); no decomposition. For CPU runs.
full mode — Mercer's own 6-stage MercerPipeline (3 candidates at T=0/0.2/0.3 + decomposition).
Every candidate goes through our validator (GuardedSandbox) before EXPLAIN or execution.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from bankql.analytics.mercer.core.models import AnnotatedSchema, ColumnSchema, ExecutionResult, RawSchema, SQLCandidate, TableSchema
from bankql.analytics.mercer.db.sandbox import ReadOnlySandbox
from bankql.connectors import QueryRejected, QueryTimeout, Source, jsonable
from bankql.guardrails import repair_reserved_tables
from bankql.semantic.mercer_mappings import write_mappings

_QUOTE = {"mysql": "`order`", "bigquery": "`order`", "tsql": "[order]"}


@dataclass
class AnalyticsResult:
    sql: str | None  # validated SQL, or None when no candidate survived
    ok: bool
    error: str = ""
    mode: str = "fast"
    stage_timings_ms: dict[str, float] = field(default_factory=dict)
    candidates: list[dict] = field(default_factory=list)  # [{strategy, sql, success, error, score}]
    tables: list[str] = field(default_factory=list)
    corrections: int = 0


class AnalyticsEngine(Protocol):
    def generate(self, question: str, *, source: Source, evidence: str | None = None,
                 feedback: str | None = None) -> AnalyticsResult: ...


def run_sync(coro):
    """Run a coroutine from sync code, also when an event loop is already running (FastAPI, MCP)."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        return pool.submit(asyncio.run, coro).result()


# ------------------------------------------------------------------------------------------- adapters

class DialectBackend:
    """Wraps a Mercer LLMBackend: Mercer's prompts never name the SQL dialect, so append it (plus quoting of the
    reserved table "order") to every system prompt. Mercer's prompt files stay untouched."""

    def __init__(self, inner, dialect: str):
        self.inner = inner
        self.note = (f"\nTarget SQL dialect: {dialect}. Write SQL that runs on {dialect}. The table named order is a "
                     f"reserved word: always write it as {_QUOTE.get(dialect, chr(34) + 'order' + chr(34))}.")

    async def generate(self, prompt, system, temperature=0.0, max_tokens=1024, is_json=False):
        return await self.inner.generate(prompt, system + self.note, temperature, max_tokens, is_json)

    async def generate_batch(self, prompts, system, temperature=0.0, max_tokens=1024, is_json=False):
        return await self.inner.generate_batch(prompts, system + self.note, temperature, max_tokens, is_json)


class GuardedSandbox(ReadOnlySandbox):
    """Mercer's sandbox interface, executing through our Source: validator first (incl. PII lineage and the
    large-table rule), then our read-only transaction, timeout and row cap. Rejections become ordinary
    failed results so Mercer's taxonomy corrector can try to fix them."""

    def __init__(self, source: Source):
        self.source = source

    def prevalidate(self, sql: str) -> tuple[bool, str]:
        v = self.source.validate(repair_reserved_tables(sql, ["order"]))
        return (True, v.sql) if v.ok else (False, f"rejected by validator ({v.code}): {v.reason}")

    async def execute(self, sql, engine):  # noqa: ARG002 — the Source owns its engine
        ok, checked = self.prevalidate(sql)
        if not ok:
            return ExecutionResult(success=False, sql=sql, row_count=0, columns=[], sample_rows=[], error_message=checked)
        t0 = time.perf_counter()
        try:
            r = await asyncio.to_thread(self.source.run, checked, None, max_rows=100)
        except (QueryRejected, QueryTimeout, Exception) as e:  # noqa: BLE001
            return ExecutionResult(success=False, sql=checked, row_count=0, columns=[], sample_rows=[],
                                   error_message=str(e)[:500], execution_time_ms=round((time.perf_counter() - t0) * 1000, 2))
        rows = [dict(zip(r.columns, [jsonable(v) for v in row])) for row in r.rows]
        return ExecutionResult(success=True, sql=checked, row_count=len(rows), columns=r.columns,
                               sample_rows=rows[:3], execution_time_ms=r.elapsed_ms)


# ------------------------------------------------------------------------------------------- engine

class MercerEngine:
    def __init__(self, backend, *, mode: str = "fast", work_dir: str | Path = "data/mercer", schema_cache=None):
        if mode not in ("fast", "full"):
            raise ValueError("mode must be fast or full")
        self.backend, self.mode = backend, mode
        self.work_dir = Path(work_dir)
        self.schema_cache = schema_cache  # Mercer SchemaCache (Redis); optional
        self._schemas: dict[str, AnnotatedSchema] = {}

    # --- schema: our allowlisted catalog + semantic joins as FKs + Mercer's value sampling + Mercer's mapper
    def schema_for(self, source: Source) -> AnnotatedSchema:
        if source.name not in self._schemas:
            self._schemas[source.name] = run_sync(self._load_schema(source))
        return self._schemas[source.name]

    async def _load_schema(self, source: Source) -> AnnotatedSchema:
        from bankql.analytics.mercer.schema.introspector import _sample_values_for_tables
        from bankql.analytics.mercer.schema.semantic_mapper import SemanticMapper

        key = hashlib.sha256(f"{source.name}|{source.cfg.url_env}|{sorted(source.cfg.tables)}|"
                             f"{source.semantic.model_dump_json()}".encode()).hexdigest()[:16]
        if self.schema_cache is not None:
            try:
                cached = await self.schema_cache.get(key)
            except Exception:  # noqa: BLE001 — Redis down: introspect instead
                cached = None
            if cached is not None:
                return cached
        fks = {}
        for a, b in source.semantic.joins:
            (ta, ca), (tb, cb) = a.split("."), b.split(".")
            fks[(ta.lower(), ca.lower())] = (tb, cb)
        tables = []
        for meta in source.catalog.tables.values():
            cols = []
            for c, t in meta.columns.items():
                ref = fks.get((meta.name.lower(), c.lower()))
                cols.append(ColumnSchema(name=c, type=t or "TEXT", is_primary_key=c.lower() == f"{meta.name.lower()}_id",
                                         is_foreign_key=ref is not None, references_table=ref and ref[0],
                                         references_column=ref and ref[1]))
            tables.append(TableSchema(name=meta.name, columns=cols))
        with source.engine.connect() as con:
            _sample_values_for_tables(con, tables)
        for t in tables:  # never put PII values into prompts as inline samples
            meta = source.catalog.tables[(None, t.name.lower())]
            for c in t.columns:
                if c.name.lower() in meta.pii:
                    c.sample_values = []
        path = write_mappings(source.semantic, self.work_dir / f"{source.name}_mappings.yaml",
                              {m.name: list(m.columns) for m in source.catalog.tables.values()})
        schema = SemanticMapper(str(path)).annotate(RawSchema(db_url_hash=key, tables=tables))
        if self.schema_cache is not None:
            try:
                await self.schema_cache.set(key, schema)
            except Exception:  # noqa: BLE001
                pass
        return schema

    # --- generation
    def generate(self, question: str, *, source: Source, evidence: str | None = None,
                 feedback: str | None = None) -> AnalyticsResult:
        q = question
        if evidence:
            q += f"\nHint: {evidence}"
        if feedback:
            q += f"\nA previous SQL attempt was rejected: {feedback}. Write a query that avoids this."
        schema = self.schema_for(source)
        return run_sync(self._full(q, source, schema) if self.mode == "full" else self._fast(q, source, schema))

    async def _fast(self, question: str, source: Source, schema: AnnotatedSchema) -> AnalyticsResult:
        from bankql.analytics.mercer.core.candidate_generator import _strip_fences
        from bankql.analytics.mercer.core.corrector import TaxonomyCorrector
        from bankql.analytics.mercer.core.entity_retriever import EntityRetriever
        from bankql.analytics.mercer.core.executor import CandidateExecutor
        from bankql.analytics.mercer.core.query_decomposer import _FALLBACK_PLAN
        from bankql.analytics.mercer.core.schema_linker import CHESSSchemaLinker
        from bankql.analytics.mercer.prompts.sql_generation import SYSTEM_PROMPT, build_prompt_direct_cot
        from bankql.analytics.mercer.schema.graph_builder import FKGraphBuilder

        backend, sandbox, timings = DialectBackend(self.backend, source.dialect), GuardedSandbox(source), {}
        t = time.perf_counter()
        ectx = EntityRetriever(schema=schema, glossary=schema.glossary).retrieve(question)
        timings["entity_retrieval"] = _ms(t)
        t = time.perf_counter()
        filtered = await CHESSSchemaLinker(llm_backend=backend, fk_graph=FKGraphBuilder(schema)).link(question, ectx, schema)
        timings["schema_linking"] = _ms(t)
        t = time.perf_counter()
        raw = await backend.generate(build_prompt_direct_cot(ectx.expanded_question or question, filtered, _FALLBACK_PLAN),
                                     SYSTEM_PROMPT, temperature=0.0, max_tokens=1024)
        cand = SQLCandidate(sql=_strip_fences(raw), strategy="direct_cot")
        timings["candidate_generation"] = _ms(t)
        t = time.perf_counter()
        scored = await CandidateExecutor(sandbox, source.engine).execute_and_score([cand])
        best = scored[0]
        timings["execution_scoring"] = _ms(t)
        res = _result(self.mode, [best], filtered, timings)
        if not res.ok:
            t = time.perf_counter()
            fixed, log = await TaxonomyCorrector(backend, sandbox, source.engine).correct(
                failed_candidate=best, question=question, filtered_schema=filtered)
            timings["correction"] = _ms(t)
            res.corrections = len(log)
            if fixed is not None:
                ok, checked = sandbox.prevalidate(fixed)
                res.sql, res.ok, res.error = (checked, True, "") if ok else (None, False, checked)
        return res

    async def _full(self, question: str, source: Source, schema: AnnotatedSchema) -> AnalyticsResult:
        from bankql.analytics.mercer.core.pipeline import MercerPipeline

        sandbox = GuardedSandbox(source)
        pipe = MercerPipeline(source.engine, backend=DialectBackend(self.backend, source.dialect),
                              annotated_schema=schema, sandbox=sandbox, cache=_NoCache())
        state = await pipe.run(question)
        res = _result("full", state.candidates, state.filtered_schema, dict(state.stage_timings))
        res.corrections = len(state.correction_log)
        if state.final_sql:
            ok, checked = sandbox.prevalidate(state.final_sql)
            res.sql, res.ok, res.error = (checked, True, "") if ok else (None, False, checked)
        else:
            res.sql, res.ok = None, False
        return res


class _NoCache:
    async def get(self, _):
        return None

    async def set(self, *_):
        return None


def _ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 1)


def _result(mode, candidates, filtered, timings) -> AnalyticsResult:
    cands = [{"strategy": c.strategy, "sql": c.sql, "score": c.score,
              "success": bool(c.execution_result and c.execution_result.success),
              "error": (c.execution_result.error_message if c.execution_result else None)} for c in candidates]
    win = next((c for c in sorted(candidates, key=lambda c: -c.score) if c.execution_result and c.execution_result.success), None)
    err = "" if win else next((c["error"] for c in cands if c["error"]), "no candidate SQL")
    return AnalyticsResult(win.execution_result.sql if win else None, win is not None, err or "", mode, timings, cands,
                           [t.name for t in (filtered.tables if filtered else [])])


def build_engine(cfg, backend=None) -> MercerEngine:
    """Default engine from config: Mercer's own llama.cpp backend on the SQL endpoint (Arctic)."""
    if backend is None:
        from bankql.analytics.mercer.inference.llamacpp_backend import LlamaCppBackend

        backend = LlamaCppBackend(base_url=cfg.sql.url().removesuffix("/v1"))
        if cfg.sql.api_key():  # Mercer's backend has no auth option; its httpx client takes default headers
            backend._client.headers["Authorization"] = f"Bearer {cfg.sql.api_key()}"
    cache = None
    try:
        from bankql.analytics.mercer.schema.cache import SchemaCache

        cache = SchemaCache(redis_url=cfg.sql.redis(), ttl_seconds=3600)
    except Exception:  # noqa: BLE001 — Redis optional at construction; failures surface on first use
        cache = None
    return MercerEngine(backend, mode=cfg.sql.mode, work_dir=cfg.path("data/mercer"), schema_cache=cache)
