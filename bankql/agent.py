"""The agent: SPEC.md section 2 flow.

question → input guardrail → (follow-up rewrite) → router
  ├─ lookup    → deterministic resolver → fixed parameterized tools
  └─ analytics → AnalyticsEngine (Mercer) → validator (≤ 2 attempts) → read-only execution + export
→ plausibility checks → output guardrail → answer (rows from the DB, SQL, 1–2 line summary)
Every question is written to the DuckDB audit log.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path

import httpx
from pydantic import BaseModel, Field

from bankql.audit import AuditLog
from bankql.config import AppConfig, load_config
from bankql.connectors import QueryRejected, QueryTimeout, Source, build_sources, jsonable
from bankql.connectors.federation import build_federations
from bankql.export import Exporter
from bankql.guardrails import SqlasGuard
from bankql.llm import LLMClient, OpenAICompatClient
from bankql.resolver import Resolver
from bankql.router import Router, ToolCall, resolve_period, ungrounded_numbers
from bankql.tools import LookupTools, ToolError, ToolResult

NOT_SURE = "I'm not sure"


class ResultSet(BaseModel):
    name: str
    sql: str
    params: dict = Field(default_factory=dict)
    columns: list[str]
    rows: list[list]  # preview (≤ preview_rows), values straight from the DB
    row_count: int
    truncated: bool = False
    export_id: str | None = None
    legend: dict = Field(default_factory=dict)  # code meanings, e.g. type: {PRIJEM: credit}
    notes: list[str] = Field(default_factory=list)


class AskResult(BaseModel):
    request_id: str
    session_id: str | None
    source: str
    question: str
    standalone_question: str
    route: str  # lookup | analytics | refused | clarify | error
    status: str  # ok | refused | needs_clarification | error
    intent: list[str] = Field(default_factory=list)
    sql: str | None = None
    columns: list[str] = Field(default_factory=list)
    rows: list[list] = Field(default_factory=list)
    row_count: int = 0
    export_id: str | None = None
    results: list[ResultSet] = Field(default_factory=list)
    summary: str = ""
    confidence: float | None = None
    router: dict = Field(default_factory=dict)  # raw decision: intent, tools+args, self/logprob confidence
    choices: list[dict] = Field(default_factory=list)
    refusal_reason: str | None = None
    timings_ms: dict[str, float] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class _Refuse(Exception):
    def __init__(self, reason: str, route: str = "refused", status: str = "refused", choices: list | None = None):
        super().__init__(reason)
        self.reason, self.route, self.status, self.choices = reason, route, status, choices or []


class Sessions:
    def __init__(self, turns: int):
        self.turns, self._d, self._lock = turns, {}, threading.Lock()

    def history(self, sid: str | None) -> list[dict]:
        with self._lock:
            return list(self._d.get(sid, [])) if sid else []

    def add(self, sid: str | None, question: str, answer: str) -> None:
        if not sid:
            return
        with self._lock:
            q = self._d.setdefault(sid, deque(maxlen=self.turns * 2))
            q.extend([{"role": "user", "content": question}, {"role": "assistant", "content": answer}])


class Agent:
    def __init__(self, cfg: AppConfig, *, router_llm: LLMClient | None = None, engine=None,
                 sources: dict[str, Source] | None = None, audit: AuditLog | None = None, exporter: Exporter | None = None):
        self.cfg = cfg
        self.guard = SqlasGuard()
        self.sources = sources or build_sources(cfg, self.guard)
        self.sources.update({k: v for k, v in build_federations(cfg, self.sources).items() if k not in self.sources})
        self.router = Router(router_llm or OpenAICompatClient(cfg.router.url(), cfg.router.model, cfg.router.timeout_s,
                                                              cfg.router.structured_output, cfg.router.api_key()),
                             use_logprobs=cfg.router.logprobs)
        if engine is None:
            from bankql.analytics import build_engine

            engine = build_engine(cfg)
        self.engine = engine
        self.audit = audit or AuditLog(cfg.path(cfg.agent.audit_path))
        self.exporter = exporter or Exporter(cfg.path(cfg.agent.exports_dir))
        self.sessions = Sessions(cfg.agent.session_turns)
        self._tools: dict[str, LookupTools] = {}
        self._resolvers: dict[str, Resolver] = {}

    @classmethod
    def from_config(cls, config_dir: str | Path = "config", **kw) -> "Agent":
        return cls(load_config(config_dir), **kw)

    def source(self, name: str) -> Source:
        if name not in self.sources:
            raise KeyError(f"unknown source {name!r}; available: {', '.join(self.sources)}")
        return self.sources[name]

    def tools(self, name: str) -> LookupTools:
        if name not in self._tools:
            self._tools[name] = LookupTools(self.source(name))
        return self._tools[name]

    def resolver(self, name: str) -> Resolver:
        if name not in self._resolvers:
            self._resolvers[name] = Resolver(self.source(name))
        return self._resolvers[name]

    # ------------------------------------------------------------------------------------------ ask

    def ask(self, question: str, source: str | None = None, session_id: str | None = None, *,
            history: list[dict] | None = None, evidence: str | None = None, surface: str = "library",
            user_id: str | None = None,
            on_event: Callable[[str, dict], None] | None = None) -> AskResult:
        t0 = time.perf_counter()
        source = source or next(iter(self.cfg.sources))
        emit = on_event or (lambda stage, data: None)
        res = AskResult(request_id=uuid.uuid4().hex, session_id=session_id, source=source, question=question,
                        standalone_question=question, route="refused", status="refused")
        timer = _Timer(res.timings_ms)
        try:
            src = self.source(source)
            with timer("input_guardrail"):
                g = self.guard.check_input(question)
            res.warnings += g.warnings
            emit("input_guardrail", {"blocked": g.blocked})
            if g.blocked:
                raise _Refuse(g.reason)
            hist = history if history is not None else self.sessions.history(session_id)
            if hist:
                with timer("rewrite"):
                    res.standalone_question, w = self.router.rewrite(question, hist)
                res.warnings += w
                emit("rewrite", {"standalone_question": res.standalone_question})
            with timer("router"):
                d = self.router.route(res.standalone_question)
            res.confidence, res.warnings = round(d.confidence, 4), res.warnings + d.warnings
            res.router = {"intent": d.intent, "tools": [{"tool": t.tool, "args": t.args()} for t in d.tools],
                          "self_confidence": d.self_confidence, "logprob_confidence": d.logprob_confidence, "reason": d.reason}
            emit("router", {"intent": d.intent, "confidence": d.confidence, "tools": [t.tool for t in d.tools]})
            if d.intent == "unparseable" or d.confidence < self.cfg.router.confidence_threshold:
                raise _Refuse(f"{NOT_SURE} what you're asking (router confidence {d.confidence:.2f}"
                              f" < {self.cfg.router.confidence_threshold}: {d.reason or d.intent}). Could you rephrase?")
            if d.intent == "unsupported":
                raise _Refuse(f"That's outside what I can answer from this bank data ({d.reason}).")
            if d.intent == "lookup":
                res.route, res.intent = "lookup", [t.tool for t in d.tools]
                with timer("lookup"):
                    self._lookup(res, src, d.tools, emit)
            else:
                res.route, res.intent = "analytics", ["analytics"]
                self._analytics(res, src, evidence, timer, emit)
            with timer("output_guardrail"):
                self._output_guard(res, src)
            res.status = "ok"
        except _Refuse as r:
            res.route, res.status, res.refusal_reason, res.choices = r.route, r.status, r.reason, r.choices
            res.summary = r.reason
        except (KeyError, ToolError) as e:
            res.refusal_reason = res.summary = str(e).strip("'\"")
        except httpx.HTTPError as e:  # llama.cpp server down or erroring: say so, don't 500
            res.route, res.status = "error", "error"
            res.refusal_reason = res.summary = (f"The local model server is not reachable or failed "
                                                f"({type(e).__name__}: {str(e)[:160]}). Is llama-server running?")
        except Exception as e:  # noqa: BLE001 — never leak a traceback to the caller; it is in the audit log
            res.route, res.status = "error", "error"
            res.refusal_reason = res.summary = f"Internal error ({type(e).__name__}); the request was logged."
            res.warnings.append(f"internal_error: {type(e).__name__}: {str(e)[:300]}")
        res.timings_ms["total"] = round((time.perf_counter() - t0) * 1000, 1)
        emit("done", {"status": res.status})
        self.sessions.add(session_id, question, res.summary)
        self.audit.write(request_id=res.request_id, session_id=session_id, user_id=user_id, surface=surface, source=source,
                         question=question, standalone_question=res.standalone_question, route=res.route,
                         intent=",".join(res.intent), sql=res.sql, row_count=res.row_count,
                         latency_ms=res.timings_ms["total"], refused=res.status != "ok",
                         refusal_reason=res.refusal_reason, warnings=res.warnings)
        return res

    # ------------------------------------------------------------------------------------------ lookup

    def _client_id(self, src: str, call: ToolCall) -> int:
        r = self.resolver(src)
        if call.client_id is not None:
            res = r.client_by_id(call.client_id)
        elif call.client_name:
            res = r.client_by_name(call.client_name, call.city)
        else:
            raise _Refuse(f"{NOT_SURE} which client you mean — please give a client id or name.")
        if res.status == "ambiguous":
            raise _Refuse(res.message, route="clarify", status="needs_clarification", choices=res.matches)
        if res.status != "found":
            raise _Refuse(res.message)
        return res.id

    def _account_id(self, src: str, call: ToolCall) -> int:
        if call.account_id is not None:
            res = self.resolver(src).account_by_id(call.account_id)
            if res.status != "found":
                raise _Refuse(res.message)
            return res.id
        cid = self._client_id(src, call)
        accts = self.tools(src).get_accounts(cid)
        if not accts.rows:
            raise _Refuse(f"Client {cid} has no accounts.")
        if len(accts.rows) > 1:
            choices = [dict(zip(accts.columns, [jsonable(v) for v in row])) for row in accts.rows]
            ids = ", ".join(str(c["account_id"]) for c in choices)
            raise _Refuse(f"Client {cid} has {len(choices)} accounts ({ids}). Which account?", route="clarify",
                          status="needs_clarification", choices=choices)
        return accts.rows[0][0]

    def _lookup(self, res: AskResult, src: Source, calls: list[ToolCall], emit) -> None:
        tools = self.tools(src.name)
        for call in calls:
            if call.tool == "get_statement":
                period = resolve_period(call.period, src.as_of())
                dfrom = call.date_from or (period[0].isoformat() if period else None)
                dto = call.date_to or (period[1].isoformat() if period else None)
                tr = tools.get_statement(self._account_id(src.name, call), dfrom, dto)
            elif call.tool == "get_orders":
                tr = tools.get_orders(self._account_id(src.name, call))
            elif call.tool == "get_loans":
                tr = (tools.get_loans(account_id=self._account_id(src.name, call)) if call.account_id is not None
                      else tools.get_loans(client_id=self._client_id(src.name, call)))
            else:
                tr = getattr(tools, call.tool)(self._client_id(src.name, call))
            emit("tool", {"tool": tr.tool, "rows": len(tr.rows)})
            res.results.append(self._result_set(tr, src.cfg.preview_rows))
        first = res.results[0]
        res.sql = ";\n".join(r.sql for r in res.results)
        res.columns, res.rows, res.row_count, res.export_id = first.columns, first.rows, first.row_count, first.export_id
        res.summary = " ".join(_lookup_summary(r) for r in res.results)

    def _result_set(self, tr: ToolResult, preview: int) -> ResultSet:
        exp = self.exporter.write_rows(tr.columns, tr.rows) if len(tr.rows) > preview else None
        return ResultSet(name=tr.tool, sql=tr.sql, params=tr.params, columns=tr.columns,
                         rows=[[jsonable(v) for v in r] for r in tr.rows[:preview]], row_count=len(tr.rows),
                         truncated=tr.truncated, export_id=exp.id if exp else None, legend=tr.legend, notes=tr.notes)

    # ------------------------------------------------------------------------------------------ analytics

    def _analytics(self, res: AskResult, src: Source, evidence, timer, emit) -> None:
        feedback, attempts = None, self.cfg.agent.max_validation_attempts
        for attempt in range(1, attempts + 1):
            with timer(f"analytics_attempt_{attempt}"):
                ar = self.engine.generate(res.standalone_question, source=src, evidence=evidence, feedback=feedback)
            for k, v in ar.stage_timings_ms.items():
                res.timings_ms[f"mercer_{k}"] = res.timings_ms.get(f"mercer_{k}", 0) + v
            emit("analytics", {"attempt": attempt, "ok": ar.ok, "error": ar.error[:200]})
            if ar.ok:
                break
            feedback = ar.error[:300]
        else:
            raise _Refuse(f"{NOT_SURE} — I couldn't produce a valid query after {attempts} attempts ({feedback}).")
        res.sql = ar.sql
        try:
            with timer("execute"):
                exp = self.exporter.export(src, ar.sql, preview_rows=src.cfg.preview_rows)
        except (QueryRejected, QueryTimeout) as e:
            raise _Refuse(f"{NOT_SURE} — the query could not be run safely ({e}).") from e
        rows = [[jsonable(v) for v in r] for r in exp.preview]
        res.results = [ResultSet(name="analytics", sql=ar.sql, columns=exp.columns, rows=rows, row_count=exp.rows,
                                 truncated=exp.truncated, export_id=exp.id,
                                 legend=src.semantic.legend(exp.columns, src.validate(ar.sql).tables))]
        res.columns, res.rows, res.row_count, res.export_id = exp.columns, rows, exp.rows, exp.id
        emit("execute", {"row_count": exp.rows})
        if exp.rows == 0:
            raise _Refuse(f"{NOT_SURE} — the query returned no rows, which may mean it misread the question. "
                          f"SQL used: {ar.sql}")
        used = set(src.validate(ar.sql).tables)
        if not used:  # e.g. a "correction" to SELECT 1: valid SQL that cannot answer a data question
            raise _Refuse(f"{NOT_SURE} — the generated query does not read any table, so it can't answer this. "
                          f"SQL: {ar.sql}")
        biggest = max((src.estimate_rows(m) for (q, n), m in src.catalog.tables.items()
                       if (f"{q}.{n}" if q else n) in used), default=0)
        if biggest and exp.rows > biggest:
            raise _Refuse(f"{NOT_SURE} — {exp.rows:,} rows is more than the largest table it reads (~{biggest:,}); "
                          f"that usually means a bad join. SQL used: {ar.sql}")
        with timer("summary"):
            res.summary = self._summary(res, src)

    def _summary(self, res: AskResult, src: Source) -> str:
        n = self.cfg.agent.summary_rows
        fallback = f"{res.row_count:,} row{'s' if res.row_count != 1 else ''} returned" + (
            f"; first: {', '.join(f'{c}={v}' for c, v in zip(res.columns, res.rows[0]))}." if res.rows else ".")
        text = self.router.summarize(res.standalone_question, res.columns, res.rows[:n], res.row_count)
        if not text:
            return fallback
        bad = ungrounded_numbers(text, res.rows[:n], [res.row_count, *res.standalone_question.split()])
        if bad:
            res.warnings.append(f"summary_ungrounded_numbers: {', '.join(bad[:5])} — replaced by a template summary")
            return fallback
        return text

    # ------------------------------------------------------------------------------------------ output

    def _output_guard(self, res: AskResult, src: Source) -> None:
        pii_cols = {c for m in src.catalog.tables.values() for c in m.pii}
        cells = [[str(v) for c, v in zip(rs.columns, row) if c.lower() in pii_cols and v is not None]
                 for rs in res.results for row in rs.rows]
        g = self.guard.check_output(res.summary, [c for c in cells if c])
        res.warnings += g.warnings
        if g.blocked:
            res.results, res.rows, res.columns = [], [], []
            raise _Refuse(f"The answer was withheld by the output guardrail ({g.reason}).")


class _Timer:
    def __init__(self, sink: dict):
        self.sink = sink

    def __call__(self, name: str):
        sink = self.sink

        class _T:
            def __enter__(self):
                self.t = time.perf_counter()

            def __exit__(self, *exc):
                sink[name] = round((time.perf_counter() - self.t) * 1000, 1)
                return False
        return _T()


def _lookup_summary(r: ResultSet) -> str:
    n = r.row_count
    what = {"get_client": "client record", "get_accounts": "account", "get_statement": "transaction",
            "get_cards": "card", "get_loans": "loan", "get_orders": "standing order"}[r.name]
    who = ", ".join(f"{k} {v}" for k, v in r.params.items() if k in ("client_id", "account_id"))
    window = f" between {r.params['date_from']} and {r.params['date_to']}" if "date_from" in r.params else ""
    return f"{n} {what}{'s' if n != 1 else ''} for {who}{window}." if n else f"No {what}s found for {who}{window}."
