import yaml

from bankql.analytics.mercer.core.models import ColumnSchema, RawSchema, TableSchema
from bankql.analytics.mercer.schema.semantic_mapper import SemanticMapper
from bankql.semantic import load_semantic
from bankql.semantic.mercer_mappings import write_mappings


def test_semantic_yaml_loads(cfg):
    m = load_semantic(cfg.sources["financial"]["semantic"])
    assert m.tables["trans"].columns["type"].valid_values["PRIJEM"] == "credit"
    assert m.tables["trans"].columns["type"].valid_values["VYDAJ"].startswith("withdrawal")
    assert m.tables["district"].columns["A11"].description == "average salary"
    assert m.pii_columns() == {("client_profile", "full_name"), ("client_profile", "phone_masked")}
    assert m.tables["trans"].time_column == "date" and m.tables["loan"].time_column == "date"
    assert m.legend(["k_symbol"])["k_symbol"]["SIPO"] == "household payment"


def test_every_db_table_and_column_is_described(src):
    m = src.semantic
    for (_, t), meta in src.catalog.tables.items():
        tab = m.table(meta.name)
        assert tab and tab.description, t
        for c in meta.columns:
            assert tab.column(c) and tab.column(c).description, f"{t}.{c}"


def test_mercer_mapper_accepts_generated_mappings(cfg, src, tmp_path):
    names = {m.name: list(m.columns) for m in src.catalog.tables.values()}
    out = write_mappings(load_semantic(cfg.sources["financial"]["semantic"]), tmp_path / "mappings.yaml", names)
    doc = yaml.safe_load(out.read_text())
    assert set(doc) == {"tables", "glossary"}
    raw = RawSchema(db_url_hash="x", tables=[
        TableSchema(name=meta.name, columns=[ColumnSchema(name=c, type=t) for c, t in meta.columns.items()])
        for meta in src.catalog.tables.values()])
    annotated = SemanticMapper(str(out)).annotate(raw)
    for t in annotated.tables:
        assert t.description, t.name
        for c in t.columns:
            assert c.description, f"{t.name}.{c.name}"
    trans_type = next(c for t in annotated.tables if t.name == "trans" for c in t.columns if c.name == "type")
    assert "'PRIJEM' = credit" in trans_type.description
    assert annotated.glossary["credit"] == "trans.type = 'PRIJEM'"
    assert "household payment" in annotated.glossary
