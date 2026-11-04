import pytest

from bankql.connectors import Source
from bankql.resolver import Resolver


@pytest.fixture
def res(src):
    return Resolver(src)


def test_ids_exact(res):
    assert res.client_by_id(12).status == "found" and res.client_by_id(12).id == 12
    assert res.client_by_id(99999).status == "not_found"
    assert res.account_by_id(5).id == 5 and res.account_by_id(0).status == "not_found"
    assert res.client_by_id("abc").status == "not_found"


def test_unique_exact_name(res):
    r = res.client_by_name("Priya Patel")
    assert r.status == "found" and r.id == 3


def test_same_name_twice_is_ambiguous(res):
    r = res.client_by_name("rahul sharma")
    assert r.status == "ambiguous" and {m["client_id"] for m in r.matches} == {1, 2}
    assert "Which one?" in r.message


def test_city_disambiguates(res):
    city = res.client_by_name("Rahul Sharma").matches[0]["city"]
    other = res.client_by_name("Rahul Sharma").matches[1]["city"]
    if city == other:
        pytest.skip("planted duplicates landed in the same city")
    r = res.client_by_name("Rahul Sharma", city=city)
    assert r.status == "found"


def test_typo_between_two_close_names_is_ambiguous(res):
    r = res.client_by_name("Priya Patal")
    assert r.status == "ambiguous" and {3, 4} <= {m["client_id"] for m in r.matches}


def test_typo_with_one_close_name_resolves(res):
    r = res.client_by_name("Ananya Iyar")
    assert r.status == "found" and r.id == 5


def test_unknown_name(res):
    assert res.client_by_name("Zzyzx Qwerty").status == "not_found"


def test_source_without_names(sources):
    s = sources["financial"]
    anon = Source("anon", s.cfg.model_copy(update={"tables": [t for t in s.cfg.tables if t != "client_profile"]}), s.semantic)
    r = Resolver(anon).client_by_name("Rahul Sharma")
    assert r.status == "unsupported" and "client id" in r.message
