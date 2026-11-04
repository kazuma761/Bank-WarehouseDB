"""FakeLLM scripts that answer the ported Mercer stages and the router deterministically."""
import json
import re

from bankql.llm import FakeLLM

PLAN = json.dumps({"aggregations": [], "filters": [], "joins": [], "ordering": None, "subqueries": [], "reasoning": "fake"})


def mercer_fake(sql_by_question: dict[str, str], *, tables: list[str] | None = None,
                fix: str | None = None) -> FakeLLM:
    """sql_by_question: substring of the question -> SQL the 'model' writes. `fix` answers correction prompts."""
    def table_sel(messages):
        return json.dumps({"selected_tables": tables or ["loan", "account", "trans", "client", "disp", "district", "order", "card"]})

    def sql(messages):
        prompt = messages[-1]["content"]
        for q, s in sql_by_question.items():
            if q.lower() in prompt.lower():
                return s
        return "SELECT 1"

    return FakeLLM(rules=[
        (r"select ONLY the tables needed", table_sel),
        (r"identify the minimum set of columns", json.dumps({"selected_columns": {}})),
        (r"SQL planning expert", PLAN),
        (r"expert SQL debugger", fix or "SELECT 1"),
        (r"expert SQL engineer", sql),
    ])


def stages(fake: FakeLLM) -> list[str]:
    names = []
    for c in fake.calls:
        sys = c["messages"][0]["content"]
        for key, name in [("select ONLY the tables", "tables"), ("minimum set of columns", "columns"),
                          ("SQL planning expert", "plan"), ("expert SQL debugger", "correction"), ("expert SQL engineer", "sql")]:
            if key in sys:
                names.append(name)
                break
        else:
            names.append(re.sub(r"\s+", " ", sys)[:30])
    return names


def route(intent="lookup", tools=(), confidence=0.95, reason="fake"):
    full = [{"tool": t.pop("tool"), **{k: None for k in ("client_id", "account_id", "client_name", "city", "date_from",
                                                          "date_to", "period")}, **t} for t in (dict(x) for x in tools)]
    return json.dumps({"intent": intent, "tools": full, "confidence": confidence, "reason": reason})


def agent_fake(routes: dict[str, str], *, rewrites: dict[str, str] | None = None, summary: str | None = None,
               logprob: float | None = None) -> FakeLLM:
    """Router-side FakeLLM: `routes` maps a question substring -> router JSON (see `route`)."""
    def router(messages):
        q = messages[-1]["content"].lower()
        for k, v in routes.items():
            if k.lower() in q:
                return v
        return route("unsupported", confidence=0.9, reason="no fake route")

    def rewrite(messages):
        latest = messages[-1]["content"].rsplit("Latest message:", 1)[-1].strip()
        out = (rewrites or {}).get(latest)
        return json.dumps({"is_follow_up": out is not None, "standalone_question": out or latest})

    return FakeLLM(rules=[
        (r"You route questions", router),
        (r"Rewrite the user's latest message", rewrite),
        (r"Answer the question in 1-2 plain sentences", json.dumps({"summary": summary or "Here are the results."})),
    ], logprob=logprob)
