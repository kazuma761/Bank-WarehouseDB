"""REST API (FastAPI) + OpenAI-compatible chat endpoint. OpenAPI spec at /openapi.json, docs at /docs.

    uvicorn bankql.api:app --port 8000          # builds the Agent from ./config
"""
from __future__ import annotations

import hmac
import json
import os
import queue
import threading
import time
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from bankql.agent import Agent, AskResult
from bankql.connectors import QueryRejected, QueryTimeout, jsonable

MODEL_NAME = "bankql"
CHAT_PREVIEW_ROWS = 20


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    source: str | None = None
    session_id: str | None = None
    evidence: str | None = None
    stream: bool = False


class SqlRequest(BaseModel):
    source: str
    sql: str = Field(min_length=1, max_length=20_000)
    max_rows: int = Field(default=100, ge=1, le=10_000)


def _keys() -> list[bytes]:
    return [k.strip().encode() for k in os.environ.get("BANKQL_API_KEYS", "").split(",") if k.strip()]


def require_key(x_api_key: str | None = Header(default=None), authorization: str | None = Header(default=None)) -> None:
    """X-API-Key or `Authorization: Bearer` (OpenAI clients). Constant-time compare against every key; with no
    keys configured, everything except /health is refused (fail closed)."""
    given = (x_api_key or (authorization or "").removeprefix("Bearer ").strip()).encode()
    ok = False
    for k in _keys():
        ok |= hmac.compare_digest(given, k)
    if not ok:
        raise HTTPException(401, "missing or invalid API key", headers={"WWW-Authenticate": "Bearer"})


def create_app(agent: Agent) -> FastAPI:
    app = FastAPI(title="BankQL", version="0.1.0",
                  description="Local-LLM, read-only natural-language → SQL for bank data.")
    auth = [Depends(require_key)]

    @app.get("/health")
    def health():
        return {"status": "ok", "sources": list(agent.sources)}

    @app.post("/v1/ask", response_model=AskResult, dependencies=auth,
              responses={200: {"content": {"text/event-stream": {}}, "description": "JSON, or SSE when stream=true"}})
    def ask(req: AskRequest):
        if not req.stream:
            return agent.ask(req.question, req.source, req.session_id, evidence=req.evidence, surface="rest")
        q: queue.Queue = queue.Queue()

        def work():
            try:
                r = agent.ask(req.question, req.source, req.session_id, evidence=req.evidence, surface="rest",
                              on_event=lambda stage, data: q.put(("stage", {"stage": stage, **data})))
                q.put(("result", r.model_dump()))
            except Exception as e:  # noqa: BLE001
                q.put(("error", {"error": str(e)[:500]}))
            q.put(None)

        threading.Thread(target=work, daemon=True).start()

        def events():
            while (item := q.get()) is not None:
                yield f"event: {item[0]}\ndata: {json.dumps(item[1], default=str)}\n\n"
        return StreamingResponse(events(), media_type="text/event-stream")

    @app.get("/v1/sources", dependencies=auth)
    def sources():
        out = []
        for n, s in agent.sources.items():
            try:
                as_of, available = str(s.as_of()), True
            except Exception:  # noqa: BLE001 — e.g. its URL env var is not set
                as_of, available = None, False
            out.append({"name": n, "dialect": s.dialect, "description": s.cfg.description, "as_of": as_of,
                        "available": available})
        return out

    @app.get("/v1/sources/{source}/tables", dependencies=auth)
    def tables(source: str):
        src = _src(agent, source)
        return [src.describe_table(m.name) for m in src.catalog.tables.values()] if not hasattr(src, "members") else \
            [{"table": f"{q}.{m.name}", "description": ""} for (q, _), m in src.catalog.tables.items()]

    @app.post("/v1/sql/validate", dependencies=auth)
    def validate(req: SqlRequest):
        v = _src(agent, req.source).validate(req.sql)
        return {"ok": v.ok, "sql": v.sql, "code": v.code, "reason": v.reason, "tables": v.tables, "warnings": v.warnings}

    @app.post("/v1/sql/run", dependencies=auth)
    def run(req: SqlRequest):
        src = _src(agent, req.source)
        t0 = time.perf_counter()
        try:
            exp = agent.exporter.export(src, req.sql, preview_rows=req.max_rows)
        except QueryRejected as e:
            raise HTTPException(400, {"code": e.validation.code, "reason": e.validation.reason}) from e
        except QueryTimeout as e:
            raise HTTPException(408, str(e)) from e
        sql = src.validate(req.sql).sql
        agent.audit.write(request_id=uuid.uuid4().hex, surface="rest_sql", source=req.source, question=None, route="sql",
                          sql=sql, row_count=exp.rows, latency_ms=round((time.perf_counter() - t0) * 1000, 1), refused=False)
        return {"sql": sql, "columns": exp.columns, "rows": [[jsonable(v) for v in r] for r in exp.preview],
                "row_count": exp.rows, "truncated": exp.truncated, "export_id": exp.id}

    @app.get("/v1/results/{export_id}.{fmt}", dependencies=auth)
    def result_file(export_id: str, fmt: str):
        p = agent.exporter.path_for(export_id, fmt)
        if p is None:
            raise HTTPException(404, "no such export")
        return FileResponse(p, media_type="text/csv" if fmt == "csv" else "application/vnd.apache.parquet",
                            filename=f"bankql-{export_id}.{fmt}")

    # ---------------------------------------------------------------------------------- OpenAI-compatible

    @app.get("/v1/models", dependencies=auth)
    def models():
        ids = [MODEL_NAME] + [f"{MODEL_NAME}:{s}" for s in agent.sources]
        return {"object": "list", "data": [{"id": i, "object": "model", "owned_by": "bankql"} for i in ids]}

    @app.post("/v1/chat/completions", dependencies=auth)
    async def chat(request: Request):
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
        import anyio

        r = await anyio.to_thread.run_sync(lambda: agent.ask(question, source, body.get("user"), history=history,
                                                             surface="openai"))
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


def _src(agent: Agent, name: str):
    try:
        return agent.source(name)
    except KeyError as e:
        raise HTTPException(404, str(e).strip("'\"")) from e


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


def __getattr__(name):  # `uvicorn bankql.api:app` builds the agent from ./config on first access
    if name == "app":
        global app
        app = create_app(Agent.from_config(os.environ.get("BANKQL_CONFIG_DIR", "config")))
        return app
    raise AttributeError(name)
