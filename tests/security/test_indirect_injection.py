"""D5: instructions planted in DATABASE VALUES must not change routing, SQL or the summary."""
from bankql.analytics import MercerEngine
from bankql.tools import LookupTools
from tests.fakes import agent_fake, mercer_fake, route
from tests.security.conftest import INJECTION, all_prompt_text


def test_injected_values_never_reach_mercer_prompts(injected_source, tmp_path):
    fake = mercer_fake({"districts": "SELECT A3, COUNT(*) AS n FROM district GROUP BY A3"})
    eng = MercerEngine(fake, work_dir=tmp_path)
    schema = eng.schema_for(injected_source)
    a2 = next(c for t in schema.tables if t.name == "district" for c in t.columns if c.name == "A2")
    assert a2.sample_values and INJECTION not in a2.sample_values  # other districts still sampled
    r = eng.generate("how many districts per region?", source=injected_source)
    text = all_prompt_text(fake)
    assert r.ok and "Ignore previous instructions" not in text and "evil.example" not in text and "developer mode" not in text


def test_routing_and_sql_unchanged_by_injected_row(make_agent, injected_source, sources, tmp_path):
    sources = dict(sources, injected=injected_source)
    router = agent_fake({"client 12": route(tools=[{"tool": "get_client", "client_id": 12}])})
    a = make_agent(router)
    a.sources["injected"] = injected_source
    r = a.ask("client 12 details please", source="injected")
    assert r.status == "ok" and r.route == "lookup" and r.sql == LookupTools(injected_source).get_client(12).sql
    assert "developer mode" in str(r.rows)  # the value is returned as data, verbatim
    assert "developer mode" not in all_prompt_text(router)  # and never shown to the router


def test_injected_cells_filtered_from_summary_prompt_and_obedient_summary_rejected(make_agent, injected_source):
    router = agent_fake({"district names": route("analytics")}, summary=INJECTION)  # a model that obeyed
    a = make_agent(router, {"district names": "SELECT district_id, A2 FROM district WHERE district_id <= 3 ORDER BY 1"})
    a.sources["injected"] = injected_source
    r = a.ask("show the first district names", source="injected")
    summary_prompt = next(c for c in router.calls if "Answer the question in 1-2" in c["messages"][0]["content"])
    assert "[filtered]" in summary_prompt["messages"][-1]["content"] and INJECTION not in summary_prompt["messages"][-1]["content"]
    assert r.status == "ok" and INJECTION not in r.summary and any("summary_rejected" in w for w in r.warnings)
    assert r.sql.startswith("SELECT district_id, A2 FROM district")
