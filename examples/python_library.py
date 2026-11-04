"""Use BankQL as a Python library. Needs llama-server(s) running — see README."""
from bankql import Agent

agent = Agent.from_config("config/")
r = agent.ask("show client 12's accounts and last month's statement", source="financial", session_id="demo")
print(r.route, r.status, r.confidence)
print(r.summary)
for rs in r.results:
    print(rs.name, rs.columns, rs.rows[:3], "rows:", rs.row_count, "export:", rs.export_id)
print(r.sql)

follow = agent.ask("only for 1997", source="financial", session_id="demo")  # rewritten into a standalone question
print(follow.standalone_question, follow.status, follow.refusal_reason or follow.summary)
