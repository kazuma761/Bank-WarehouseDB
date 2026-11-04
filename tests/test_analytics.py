import pytest

from bankql.analytics import MercerEngine
from tests.fakes import mercer_fake, stages

Q = "How many loans are in debt?"
GOLD = "SELECT COUNT(*) AS n FROM loan WHERE status = 'D'"


@pytest.fixture
def engine_for(tmp_path):
    def make(fake, mode="fast"):
        return MercerEngine(fake, mode=mode, work_dir=tmp_path)
    return make


def test_fast_mode_one_candidate_no_decomposition(src, engine_for):
    fake = mercer_fake({Q: GOLD})
    r = engine_for(fake).generate(Q, source=src)
    assert r.ok and r.mode == "fast" and len(r.candidates) == 1 and r.candidates[0]["strategy"] == "direct_cot"
    assert src.run(r.sql).rows == src.run(GOLD).rows
    assert stages(fake) == ["tables", "columns", "sql"]  # no 'plan' stage
    assert all(c["temperature"] == 0.0 for c in fake.calls if "expert SQL engineer" in c["messages"][0]["content"])
    assert set(r.stage_timings_ms) >= {"entity_retrieval", "schema_linking", "candidate_generation", "execution_scoring"}


def test_full_mode_runs_mercer_pipeline(src, engine_for):
    fake = mercer_fake({Q: GOLD})
    r = engine_for(fake, "full").generate(Q, source=src)
    assert r.ok and r.mode == "full" and len(r.candidates) == 3
    assert {c["strategy"] for c in r.candidates} == {"direct_cot", "divide_conquer", "plan_execute"}
    assert "plan" in stages(fake) and stages(fake).count("sql") == 3
    assert src.run(r.sql).rows == src.run(GOLD).rows


def test_dialect_is_told_to_the_model(src, engine_for):
    fake = mercer_fake({Q: GOLD})
    engine_for(fake).generate(Q, source=src)
    assert all(f"Target SQL dialect: {src.dialect}" in c["messages"][0]["content"] for c in fake.calls)


def test_validator_rejection_feeds_mercer_corrector(src, engine_for):
    fake = mercer_fake({Q: "SELECT COUNT(*) FROM loans WHERE status = 'D'"}, fix=GOLD)
    r = engine_for(fake).generate(Q, source=src)
    assert r.ok and r.corrections >= 1 and "correction" in stages(fake)
    assert "table_not_allowed" in r.candidates[0]["error"]


def test_unfixable_large_scan_is_not_ok(src, engine_for):
    bad = "SELECT COUNT(*) FROM trans"
    r = engine_for(mercer_fake({"transactions": bad}, fix=bad)).generate("How many transactions are there?", source=src)
    assert not r.ok and r.sql is None and "unbounded_large_scan" in r.error


def test_pii_output_never_returned(src, engine_for):
    leak = "SELECT full_name FROM client_profile"
    r = engine_for(mercer_fake({"names": leak}, fix=leak)).generate("List all client names", source=src)
    assert not r.ok and "pii_output" in r.error


def test_bare_order_table_repaired(src, engine_for):
    r = engine_for(mercer_fake({"standing orders": "SELECT COUNT(*) FROM order"})).generate("How many standing orders?", source=src)
    assert r.ok and src.run(r.sql).rows[0][0] == 519


def test_schema_has_samples_but_no_pii_samples(src, engine_for):
    schema = engine_for(mercer_fake({})).schema_for(src)
    cols = {(t.name.lower(), c.name.lower()): c for t in schema.tables for c in t.columns}
    assert {"PRIJEM", "VYDAJ"} <= set(cols[("trans", "type")].sample_values)
    assert cols[("client_profile", "full_name")].sample_values == []
    assert cols[("disp", "account_id")].is_foreign_key and cols[("disp", "account_id")].references_table == "account"
    assert "credit" in cols[("trans", "type")].description


def test_evidence_and_feedback_reach_the_prompt(src, engine_for):
    fake = mercer_fake({Q: GOLD})
    engine_for(fake).generate(Q, source=src, evidence="in debt refers to status = 'D'", feedback="row explosion")
    sql_prompt = next(c for c in fake.calls if "expert SQL engineer" in c["messages"][0]["content"])["messages"][-1]["content"]
    assert "status = 'D'" in sql_prompt and "row explosion" in sql_prompt


async def test_redis_schema_cache_roundtrip(sqlite_src, tmp_path):
    import fakeredis

    from bankql.analytics.mercer.schema.cache import SchemaCache

    cache = SchemaCache._from_client(fakeredis.aioredis.FakeRedis(decode_responses=True))
    eng = MercerEngine(mercer_fake({}), work_dir=tmp_path, schema_cache=cache)
    first = await eng._load_schema(sqlite_src)
    again = await eng._load_schema(sqlite_src)
    assert again.model_dump() == first.model_dump()
