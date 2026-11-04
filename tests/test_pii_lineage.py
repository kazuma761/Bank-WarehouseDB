"""Decision Q4: analytics SQL may not OUTPUT PII; enforced on column lineage, not on bare names.
Filtering on PII and COUNT / COUNT(DISTINCT) over PII stay allowed."""
import pytest

LEAKS = {
    "direct": "SELECT full_name FROM client_profile",
    "aliased": "SELECT p.full_name AS who FROM client_profile p",
    "concat": "SELECT 'Dear ' || full_name AS greeting FROM client_profile",
    "concat_fn": "SELECT CONCAT(full_name, ' / ', city) AS label FROM client_profile",
    "case_branch": "SELECT CASE WHEN city = 'Pune' THEN full_name ELSE 'other' END AS v FROM client_profile",
    "case_default": "SELECT CASE WHEN city = 'Pune' THEN 'x' ELSE phone_masked END AS v FROM client_profile",
    "group_by_and_select": "SELECT full_name, COUNT(*) AS n FROM client_profile GROUP BY full_name",
    "min_max": "SELECT MIN(full_name) AS first_name FROM client_profile",
    "substr": "SELECT SUBSTR(full_name, 1, 3) AS initials FROM client_profile",
    "upper": "SELECT UPPER(full_name) AS u FROM client_profile",
    "coalesce": "SELECT COALESCE(phone_masked, 'none') AS ph FROM client_profile",
    "star": "SELECT * FROM client_profile",
    "table_star": "SELECT p.* FROM client_profile p",
    "cte": "WITH p AS (SELECT full_name AS nm FROM client_profile) SELECT nm FROM p",
    "nested_cte": "WITH a AS (SELECT full_name AS x FROM client_profile), b AS (SELECT x AS y FROM a) SELECT y FROM b",
    "derived": "SELECT s.n FROM (SELECT full_name AS n FROM client_profile) s",
    "scalar_subquery": "SELECT c.client_id, (SELECT p.full_name FROM client_profile p WHERE p.client_id = c.client_id) AS who FROM client c",
    "union_second_branch": "SELECT city FROM client_profile UNION SELECT full_name FROM client_profile",
    "window_lag": "SELECT LAG(full_name) OVER (ORDER BY client_id) AS prev FROM client_profile",
    "join": "SELECT c.gender, p.full_name FROM client c JOIN client_profile p ON p.client_id = c.client_id",
}

ALLOWED = {
    "count": "SELECT COUNT(full_name) AS n FROM client_profile",
    "count_distinct": "SELECT COUNT(DISTINCT full_name) AS n FROM client_profile",
    "count_filtered": "SELECT COUNT(*) AS n FROM client_profile WHERE full_name LIKE 'R%'",
    "count_by_city": "SELECT city, COUNT(DISTINCT full_name) AS names FROM client_profile GROUP BY city",
    "non_pii_cols": "SELECT client_id, city FROM client_profile",
    "exists": "SELECT COUNT(*) FROM client c WHERE EXISTS (SELECT 1 FROM client_profile p WHERE p.client_id = c.client_id AND p.full_name = 'Rahul Sharma')",
}

# Allowed before the small-count rule; each lets a caller learn something about ONE person, so now refused.
SMALL_COUNT_REFUSED = {
    "row_level_filter": "SELECT c.client_id, c.gender FROM client c JOIN client_profile p ON p.client_id = c.client_id WHERE p.full_name LIKE 'Rahul%'",
    "boolean_compare": "SELECT client_id, full_name = 'Rahul Sharma' AS is_rahul FROM client_profile",
    "case_condition": "SELECT CASE WHEN full_name LIKE 'A%' THEN 1 ELSE 0 END AS starts_a FROM client_profile",
    "partition_by": "SELECT client_id, ROW_NUMBER() OVER (PARTITION BY full_name ORDER BY client_id) AS rn FROM client_profile",
    "group_by_pii": "SELECT COUNT(*) AS n FROM client_profile GROUP BY full_name HAVING COUNT(*) > 1",
    "or_probe": "SELECT COUNT(*) FROM client_profile WHERE client_id <= 5 OR (client_id = 12 AND full_name LIKE 'A%')",
    "not_probe": "SELECT COUNT(*) FROM client_profile WHERE NOT full_name LIKE 'A%'",
    "or_around_subquery": "SELECT COUNT(*) FROM client c WHERE c.client_id <= 5 OR EXISTS (SELECT 1 FROM client_profile p WHERE p.client_id = c.client_id AND p.client_id = 12 AND p.full_name LIKE 'A%')",
    "sum_filtered": "SELECT SUM(c.district_id) AS s FROM client c JOIN client_profile p ON p.client_id = c.client_id WHERE p.full_name LIKE 'R%'",
    # Known false refusal: the output check looks at the outermost SELECT only.
    "count_in_cte": "WITH n AS (SELECT city, COUNT(DISTINCT full_name) AS names FROM client_profile GROUP BY city) SELECT city, names FROM n",
}


@pytest.mark.parametrize("case", sorted(SMALL_COUNT_REFUSED))
def test_small_count_rule_refuses_per_person_shapes(src, case):
    v = src.validate(SMALL_COUNT_REFUSED[case], allow_pii_filter=True)  # even a key allowed to filter on PII
    assert not v.ok and v.code == "pii_filter_shape", (case, v.code, v.reason)


def test_small_counts_are_bucketed(src):
    r = src.run("SELECT COUNT(*) AS n FROM client_profile WHERE full_name = 'Priya Patel'", allow_pii_filter=True)
    assert r.rows == [["<5"]]
    big = src.run("SELECT COUNT(*) AS n FROM client_profile WHERE full_name LIKE '%a%'", allow_pii_filter=True)
    assert isinstance(big.rows[0][0], int) and big.rows[0][0] >= 5
    plain = src.run("SELECT COUNT(*) AS n FROM loan WHERE status = 'D'")  # no PII: untouched
    assert isinstance(plain.rows[0][0], int)


@pytest.mark.parametrize("case", sorted(LEAKS))
def test_pii_leak_is_blocked(src, case):
    v = src.validate(LEAKS[case])
    assert not v.ok and v.code == "pii_output", (case, v.code, v.reason)


@pytest.mark.parametrize("case", sorted(ALLOWED))
def test_pii_filter_and_count_allowed(src, case):
    v = src.validate(ALLOWED[case], allow_pii_filter=True)
    assert v.ok, (case, v.code, v.reason)
    src.run(ALLOWED[case], max_rows=3, allow_pii_filter=True)


FILTERS_PII = {"count_filtered", "exists"}


@pytest.mark.parametrize("case", sorted(ALLOWED))
def test_default_key_may_not_filter_on_pii(src, case):
    """Default deny: without the pii_filter permission a PII column may not appear in WHERE/HAVING/JOIN-ON.
    Counting PII without filtering on it stays allowed (still bucketed)."""
    v = src.validate(ALLOWED[case])
    if case in FILTERS_PII:
        assert not v.ok and v.code == "pii_filter_denied", (case, v.code)
    else:
        assert v.ok, (case, v.code, v.reason)


def test_lookup_context_may_return_pii(src):
    assert src.validate(LEAKS["direct"], allow_pii_output=True).ok
