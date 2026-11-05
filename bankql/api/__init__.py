"""REST API (FastAPI) + OpenAI-compatible chat endpoint. OpenAPI spec at /openapi.json, docs at /docs.

    uvicorn bankql.api:app --port 8000          # builds the Agent from ./config
"""
from __future__ import annotations

import json
import os
import queue
import tempfile
import threading
import time
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

import anyio

from bankql import onboarding
from bankql.agent import Agent, AskResult
from bankql.auth import Forbidden, KeyStore, Principal, RateLimited
from bankql.config import local_sources
from bankql.connectors import QueryRejected, QueryTimeout, jsonable

MODEL_NAME = "bankql"
DEMO_HTML = (Path(__file__).parent / "demo.html").read_text(encoding="utf-8")
ADMIN_HTML = (Path(__file__).parent / "admin.html").read_text(encoding="utf-8")
PAGE_HEADERS = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                                           "connect-src 'self'; frame-ancestors 'none'"}
CHAT_PREVIEW_ROWS = 20


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    source: str | None = None
    session_id: str | None = None
    evidence: str | None = None
    user_id: str | None = Field(default=None, max_length=200,
                                description="Reserved for the per-user permissions phase: accepted and audited, not yet enforced.")
    stream: bool = False


class ConnectRequest(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    url: str = Field(min_length=1, max_length=2000, description="SQLAlchemy URL; stored only in .env")
    tables: list[str] | None = Field(default=None, description="allowlist; default: every table the URL shows")
    description: str = Field(default="", max_length=300)


class SqlRequest(BaseModel):
    source: str
    sql: str = Field(min_length=1, max_length=20_000)
    max_rows: int = Field(default=100, ge=1, le=10_000)


def create_app(agent: Agent, keys: KeyStore | None = None) -> FastAPI:
    """`keys`: API keys and per-key policy (default: BANKQL_API_KEYS + config/api_keys.yaml). No keys configured →
    every /v1 call is refused (fail closed)."""
    app = FastAPI(title="BankQL", version="0.1.0",
                  description="Local-LLM, read-only natural-language → SQL for bank data.")
    store = keys or KeyStore(agent.cfg.root / "config")

    def principal(x_api_key: str | None = Header(default=None), authorization: str | None = Header(default=None)) -> Principal:
        """X-API-Key or `Authorization: Bearer`."""
        p = store.authenticate(x_api_key or (authorization or "").removeprefix("Bearer ").strip())
        if p is None:
            raise HTTPException(401, "missing or invalid API key", headers={"WWW-Authenticate": "Bearer"})
        return p

    def allowed_src(p: Principal, name: str | None):
        name = name or next((n for n in agent.sources if p.policy.allows_source(n)), None)
        if name is None or not p.policy.allows_source(name):
            raise HTTPException(403, "this API key may not use that source")
        if name not in agent.sources:
            raise HTTPException(404, "unknown source")  # no list: a key only sees /v1/sources it may use
        return agent.sources[name]

    @app.exception_handler(Forbidden)
    async def _forbidden(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=403)

    @app.exception_handler(RateLimited)
    async def _limited(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=429, headers={"Retry-After": str(exc.retry_after_s)})

    @app.exception_handler(Exception)
    async def _internal(request, exc):  # never a traceback or internals in a response
        return JSONResponse({"detail": "internal error"}, status_code=500)


    @app.get("/demo", include_in_schema=False)
    def demo():
        return HTMLResponse(DEMO_HTML, headers=PAGE_HEADERS)

    @app.get("/admin", include_in_schema=False)
    def admin_page():  # static page; every action it takes needs an admin key
        return HTMLResponse(ADMIN_HTML, headers=PAGE_HEADERS)

    @app.get("/health")
    def health():
        return {"status": "ok"}  # unauthenticated: reveals nothing about sources

    @app.post("/v1/ask", response_model=AskResult,
              responses={200: {"content": {"text/event-stream": {}}, "description": "JSON, or SSE when stream=true"},
                         403: {"description": "source not allowed for this key"}, 429: {"description": "rate limited"}})
    def ask(req: AskRequest, p: Principal = Depends(principal)):
        src = allowed_src(p, req.source)
        kw = dict(evidence=req.evidence, surface="rest", user_id=req.user_id, principal=p)
        if not req.stream:
            return agent.ask(req.question, src.name, req.session_id, **kw)
        q: queue.Queue = queue.Queue()

        def work():
            try:
                r = agent.ask(req.question, src.name, req.session_id,
                              on_event=lambda stage, data: q.put(("stage", {"stage": stage, **data})), **kw)
                q.put(("result", r.model_dump()))
            except RateLimited as e:
                q.put(("error", {"error": str(e), "status": 429}))
            except Exception:  # noqa: BLE001 — no internals in the stream
                q.put(("error", {"error": "internal error"}))
            q.put(None)

        threading.Thread(target=work, daemon=True).start()

        def events():
            while (item := q.get()) is not None:
                yield f"event: {item[0]}\ndata: {json.dumps(item[1], default=str)}\n\n"
        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/v1/sources")
    def sources(p: Principal = Depends(principal)):
        out = []
        for n, s in agent.sources.items():
            if not p.policy.allows_source(n):
                continue
            try:
                as_of, available = str(s.as_of()), True
            except Exception:  # noqa: BLE001 — e.g. its URL env var is not set
                as_of, available = None, False
            out.append({"name": n, "dialect": s.dialect, "description": s.cfg.description, "as_of": as_of,
                        "available": available})
        return out

    @app.get("/v1/sources/{source}/tables")
    def tables(source: str, p: Principal = Depends(principal)):
        src = allowed_src(p, source)
        return [src.describe_table(m.name) for m in src.catalog.tables.values()] if not hasattr(src, "members") else \
            [{"table": f"{q}.{m.name}", "description": ""} for (q, _), m in src.catalog.tables.items()]

    @app.post("/v1/sql/validate")
    def validate(req: SqlRequest, p: Principal = Depends(principal)):
        v = allowed_src(p, req.source).validate(req.sql, allow_pii_filter=p.policy.pii_filter)
        return {"ok": v.ok, "sql": v.sql, "code": v.code, "reason": v.reason, "tables": v.tables, "warnings": v.warnings}

    @app.post("/v1/sql/run", responses={403: {"description": "run_sql not enabled for this key"}})
    def run(req: SqlRequest, p: Principal = Depends(principal)):
        if not p.policy.run_sql:
            raise HTTPException(403, "run_sql is not enabled for this API key (config/api_keys.yaml)")
        src = allowed_src(p, req.source)
        agent.limiter.hit(p, "ask")
        t0 = time.perf_counter()
        try:
            exp = agent.exporter.export(src, req.sql, preview_rows=req.max_rows, owner=p.key_id,
                                        allow_pii_filter=p.policy.pii_filter)
        except QueryRejected as e:
            raise HTTPException(400, {"code": e.validation.code, "reason": e.validation.reason}) from e
        except QueryTimeout as e:
            raise HTTPException(408, str(e)) from e
        sql = src.validate(req.sql, allow_pii_filter=p.policy.pii_filter).sql
        agent.audit.write(request_id=uuid.uuid4().hex, surface=f"rest_sql:{p.key_id}", source=src.name, question=None,
                          route="sql", sql=agent.scrubber(src.name).sql(sql), row_count=exp.rows,
                          latency_ms=round((time.perf_counter() - t0) * 1000, 1), refused=False)
        return {"sql": sql, "columns": exp.columns, "rows": [[jsonable(v) for v in r] for r in exp.preview],
                "row_count": exp.rows, "truncated": exp.truncated, "export_id": exp.id}

    @app.get("/v1/results/{export_id}.{fmt}")
    def result_file(export_id: str, fmt: str, p: Principal = Depends(principal)):
        path = agent.exporter.path_for(export_id, fmt, owner=p.key_id)
        if path is None:  # unknown, expired, or another key's export: same answer
            raise HTTPException(404, "no such export")
        return FileResponse(path, media_type="text/csv" if fmt == "csv" else "application/vnd.apache.parquet",
                            filename=f"bankql-{export_id}.{fmt}")

    # ---------------------------------------------------------------------------------- plug in data (admin)

    def admin(p: Principal = Depends(principal)) -> Principal:
        if not p.policy.admin:
            raise HTTPException(403, "admin permission required (BANKQL_ADMIN_KEYS, or admin: true in config/api_keys.yaml)")
        return p

    def onboarded(p: Principal, action: str, r, t0: float) -> dict:
        agent.audit.write(request_id=uuid.uuid4().hex, surface=f"admin:{p.key_id}", source=r.name, route="admin",
                          intent=action, row_count=len(r.tables), latency_ms=round((time.perf_counter() - t0) * 1000, 1),
                          refused=not r.ok, refusal_reason=None if r.ok else "check-source failed")
        body = {"name": r.name, "ok": r.ok, "tables": r.tables, "semantic_files": r.semantic_files,
                "checks": [{"status": s, "check": c, "detail": d} for s, c, d in r.checks]}
        if not r.ok:
            raise HTTPException(400, body)
        return body

    @app.exception_handler(onboarding.OnboardError)
    async def _bad_input(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/v1/admin/sources")
    def admin_sources(p: Principal = Depends(admin)):
        local = local_sources(agent._config_dir())
        return [{"name": n, "dialect": s.dialect, "description": s.cfg.description, "tables": list(s.cfg.tables),
                 "origin": (local.get(n) or {}).get("origin", "config")} for n, s in agent.sources.items()]

    @app.post("/v1/admin/sources/inspect")
    async def admin_inspect(req: ConnectRequest, p: Principal = Depends(admin)):
        onboarding.check_name(agent._config_dir(), req.name)
        dialect, tables, checks = await anyio.to_thread.run_sync(lambda: onboarding.inspect_url(req.name, req.url))
        return {"dialect": dialect, "tables": tables,
                "checks": [{"status": s, "check": c, "detail": d} for s, c, d in checks]}

    @app.post("/v1/admin/sources")
    async def admin_connect(req: ConnectRequest, p: Principal = Depends(admin)):
        t0 = time.perf_counter()
        r = await anyio.to_thread.run_sync(lambda: agent.connect_source(req.name, req.url, req.tables, req.description))
        return onboarded(p, "connect", r, t0)

    @app.put("/v1/admin/imports/{name}/{filename}",
             openapi_extra={"requestBody": {"content": {"application/octet-stream": {}}, "required": True}})
    async def admin_import(name: str, filename: str, request: Request, p: Principal = Depends(admin)):
        """Raw file body (CSV / TSV / Parquet) → table <file stem> in source <name>; repeat for more files."""
        onboarding.check_name(agent._config_dir(), name)
        onboarding.table_name(filename)
        cap = int(float(os.environ.get("BANKQL_IMPORT_MAX_MB", "500")) * 1024 * 1024)
        tmp_dir = agent.cfg.path(agent.IMPORTS_DIR) / ".upload"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=tmp_dir, suffix=Path(filename).suffix.lower())
        try:
            size = 0
            with os.fdopen(fd, "wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > cap:
                        raise HTTPException(413, f"file larger than {cap // 1024 // 1024} MB (BANKQL_IMPORT_MAX_MB)")
                    f.write(chunk)
            if size == 0:
                raise HTTPException(400, "empty file")
            t0 = time.perf_counter()
            r = await anyio.to_thread.run_sync(lambda: agent.import_file(name, filename, Path(tmp)))
            return onboarded(p, "import", r, t0)
        finally:
            Path(tmp).unlink(missing_ok=True)

    @app.delete("/v1/admin/sources/{name}")
    def admin_remove(name: str, p: Principal = Depends(admin)):
        if not agent.remove_source(name):
            raise HTTPException(404, "no plugged-in source with that name")
        agent.audit.write(request_id=uuid.uuid4().hex, surface=f"admin:{p.key_id}", source=name, route="admin",
                          intent="remove", refused=False)
        return {"removed": name}

    # ---------------------------------------------------------------------------------- OpenAI-compatible

    @app.get("/v1/models")
    def models(p: Principal = Depends(principal)):
        ids = [MODEL_NAME] + [f"{MODEL_NAME}:{s}" for s in agent.sources if p.policy.allows_source(s)]
        return {"object": "list", "data": [{"id": i, "object": "model", "owned_by": "bankql"} for i in ids]}

    @app.post("/v1/chat/completions")
    async def chat(request: Request, p: Principal = Depends(principal)):
        body = await request.json()
        model = str(body.get("model") or MODEL_NAME)
        if not (model == MODEL_NAME or model.startswith(f"{MODEL_NAME}:")):
            raise HTTPException(404, f"model {model!r} not found; use {MODEL_NAME} or {MODEL_NAME}:<source>")
        source = model.split(":", 1)[1] if ":" in model else None
        msgs = [m for m in body.get("messages", []) if m.get("role") in ("user", "assistant")]
        if not msgs or msgs[-1]["role"] != "user":
            raise HTTPException(400, "the last message must be from the user")
        question = _text(msgs[-1]["content"])
        history = [{"role": m["role"], "content": _text(m["content"])} for m in msgs[:-1]][-12:]
        src = allowed_src(p, source)
        r = await anyio.to_thread.run_sync(lambda: agent.ask(question, src.name, body.get("user"), history=history,
                                                             surface="openai", principal=p))
        content = render_markdown(r)
        cid, created = f"chatcmpl-{r.request_id}", int(time.time())
        if body.get("stream"):
            def sse():
                for delta in ({"role": "assistant", "content": content}, {}):
                    chunk = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                             "choices": [{"index": 0, "delta": delta, "finish_reason": None if delta else "stop"}]}
                    yield f"data: {json.dumps(chunk)}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(sse(), media_type="text/event-stream")
        return {"id": cid, "object": "chat.completion", "created": created, "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, "bankql": r.model_dump()}

    return app


def _text(content) -> str:
    if isinstance(content, list):  # OpenAI content parts
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content or "")


def _md_cell(v) -> str:
    return str("" if v is None else v).replace("|", "\\|").replace("\n", " ")


def render_markdown(r: AskResult) -> str:
    out = [r.summary or ""]
    if r.choices:
        out.append("\n".join(f"- {', '.join(f'{k}: {v}' for k, v in c.items() if k != 'score')}" for c in r.choices))
    for rs in r.results:
        if len(r.results) > 1:
            out.append(f"**{rs.name}**")
        if rs.rows:
            rows = rs.rows[:CHAT_PREVIEW_ROWS]
            table = ["| " + " | ".join(rs.columns) + " |", "|" + "---|" * len(rs.columns)]
            table += ["| " + " | ".join(_md_cell(v) for v in row) + " |" for row in rows]
            out.append("\n".join(table))
            if rs.row_count > len(rows):
                more = f"Showing {len(rows)} of {rs.row_count:,} rows."
                if rs.export_id:
                    more += f" Full result: `GET /v1/results/{rs.export_id}.csv`"
                out.append(more)
        for col, codes in rs.legend.items():
            out.append(f"_{col}_: " + "; ".join(f"`{k}` = {v}" for k, v in codes.items()))
    if r.sql:
        out.append(f"```sql\n{r.sql}\n```")
    return "\n\n".join(x for x in out if x)


def __getattr__(name):  # `uvicorn bankql.api:app` builds the agent from ./config on first access (and warms it)
    if name == "app":
        global app
        agent = Agent.from_config(os.environ.get("BANKQL_CONFIG_DIR", "config"))
        threading.Thread(target=agent.warm, daemon=True, name="bankql-warm").start()
        app = create_app(agent)
        return app
    raise AttributeError(name)
