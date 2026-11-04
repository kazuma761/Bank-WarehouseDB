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
