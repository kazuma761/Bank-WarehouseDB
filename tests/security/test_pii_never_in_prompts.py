"""D1: no PII value from the database ever reaches a model prompt (M-Schema samples, entity index, Redis schema
cache, session history, client-supplied history). Names the user typed in the same message are their own input."""
import json

import fakeredis

from bankql.analytics import MercerEngine
from bankql.analytics.mercer.schema.cache import SchemaCache
from tests.fakes import agent_fake, mercer_fake, route


def _pii_values(src):
    r = src.run("SELECT full_name, phone_masked FROM client_profile", max_rows=10_000, allow_pii_output=True)
    return {v for row in r.rows for v in row}


async def test_schema_samples_index_and_cache_hold_no_pii(src, tmp_path):
    cache = SchemaCache._from_client(fakeredis.aioredis.FakeRedis(decode_responses=True))
    eng = MercerEngine(mercer_fake({}), work_dir=tmp_path, schema_cache=cache)
    schema = await eng._load_schema(src)
    blob = schema.model_dump_json() + json.dumps(await cache._client.hgetall("x") or {})
    for k in await cache._client.keys("*"):
        blob += str(await cache._client.get(k))
    pii = _pii_values(src)
    assert pii and not any(v in blob for v in pii)
    cities = next(c for t in schema.tables if t.name == "client_profile" for c in t.columns if c.name == "city")
    assert cities.sample_values  # positive control: non-PII columns are still sampled


def test_no_db_pii_in_any_prompt_over_a_conversation(make_agent, src):
    router = agent_fake({
        "rahul": route(tools=[{"tool": "get_client", "client_name": "Rahul Sharma"}]),
        "client 1 ": route(tools=[{"tool": "get_client", "client_id": 1}]),
        "client 3 ": route(tools=[{"tool": "get_accounts", "client_id": 3}]),
        "in debt": route("analytics"),
        "patal": route(tools=[{"tool": "get_client", "client_name": "Priya Patal"}]),
    }, rewrites={"the first one": "profile of client 1 please"}, summary="Here are the results.")
    a = make_agent(router, {"in debt": "SELECT COUNT(*) AS n FROM loan WHERE status = 'D'"})
    for q in ["rahul sharma ka profile", "the first one", "profile of client 1 please", "client 3 accounts",
              "How many loans are in debt?", "priya patal details"]:
        a.ask(q, source=src.name, session_id="s")
    # client-supplied (OpenAI-style) history quoting our own markdown table with a name + phone
    a.ask("and the loans in debt?", source=src.name, history=[
        {"role": "user", "content": "profile of client 1"},
        {"role": "assistant", "content": "1 client record.\n\n| full_name | phone_masked |\n|---|---|\n| Rahul Sharma | +91-XXXXXX-1234 |"}])
    prompts = all_prompt_text(router, a.engine.backend)
    typed = {"rahul sharma", "priya patal"}
    leaked = [v for v in _pii_values(src) if v.lower() in prompts.lower() and v.lower() not in typed]
    assert leaked == []


from tests.security.conftest import all_prompt_text  # noqa: E402
