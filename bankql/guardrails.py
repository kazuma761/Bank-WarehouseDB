"""Guardrails: our sqlglot read-only validator (the hard gate) plus SQLAS GuardrailPipeline with the
policies settled in SPEC.md section 10 (Q2, Q3, Q4).

Only what `SqlValidator.validate` returns as `sql` is ever executed: the SQL is re-rendered from the
validated AST, so what we checked is exactly what runs (no parser differential between sqlglot and the DB).
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import OptimizeError, ParseError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, build_scope, traverse_scope

# --------------------------------------------------------------------------------------------- catalog


@dataclass
class TableMeta:
    name: str  # canonical table name as the database reports it
    columns: dict[str, str]  # column -> type
    qualifier: str | None = None  # federation member (DuckDB catalog) the table lives in
    pii: set[str] = field(default_factory=set)  # lower-case PII column names
    time_column: str | None = None
    large: bool = False


@dataclass
class Catalog:
    """Allowlisted tables a validator may reference. Keys are lower-case (qualifier, name)."""
    tables: dict[tuple[str | None, str], TableMeta]
    default_schemas: frozenset[str] = frozenset({"main", "public", "dbo"})

    def resolve(self, t: exp.Table) -> TableMeta | None:
        parts = [p.lower() for p in (t.catalog, t.db) if p]
        name = t.name.lower()
        quals = {k[0] for k in self.tables}
        if not parts:
            return self.tables.get((None, name)) if None in quals else None
        if len(parts) == 2 and parts[1] in self.default_schemas:  # member.main.table
            parts = parts[:1]
        if len(parts) == 1:
            if parts[0] in self.default_schemas and None in quals:  # main.table / public.table
                return self.tables.get((None, name))
            return self.tables.get((parts[0], name))
        return self.tables.get((".".join(parts), name))  # Trino: catalog.schema.table

    def sqlglot_schema(self) -> dict:
        out: dict = {}
        for (q, name), meta in self.tables.items():
            cols = {c: (t or "TEXT") for c, t in meta.columns.items()}
            (out.setdefault(q, {}) if q else out)[meta.name] = cols
        return out


# --------------------------------------------------------------------------------------------- rendering

# Identifiers reserved in any dialect sqlglot knows get quoted on output (e.g. the BIRD table "order").
RESERVED = frozenset(
    w.lower() for d in ("mysql", "duckdb", "bigquery", "trino", "postgres", "sqlite", "tsql", "snowflake", "oracle")
    for w in getattr(Dialect.get_or_raise(d).generator_class, "RESERVED_KEYWORDS", set())
) | {"order", "group", "user", "table", "select", "from", "where", "limit", "check", "default", "key", "index"}
_PARAM_MARK = "__bankql_param_{}__"


def render(tree: exp.Expression, dialect: str) -> str:
    """Generate SQL for *dialect*, quoting reserved identifiers and keeping `:name` bind parameters
    (sqlglot would otherwise rewrite them to %(name)s / $name / @name)."""
    tree = tree.copy()
    names: list[str] = []
    for ph in list(tree.find_all(exp.Placeholder)):
        if ph.name:
            names.append(ph.name)
            ph.replace(exp.column(_PARAM_MARK.format(ph.name)))
    for ident in tree.find_all(exp.Identifier):
        if ident.name.lower() in RESERVED:
            ident.set("quoted", True)
    sql = tree.sql(dialect=dialect, comments=False)
    for n in names:
        sql = sql.replace(_PARAM_MARK.format(n), f":{n}")
    return sql


# --------------------------------------------------------------------------------------------- validator

_FORBIDDEN = tuple(t for t in (getattr(exp, n, None) for n in (
    "Insert", "Update", "Delete", "Merge", "Drop", "Create", "Alter", "AlterColumn", "TruncateTable", "Command",
    "Pragma", "Set", "Use", "Attach", "Detach", "Copy", "Into", "Lock", "Transaction", "Commit", "Rollback",
    "Grant", "Revoke", "LoadData", "Analyze", "Describe", "Install", "Export", "Refresh", "Cache", "Uncache",
)) if t is not None)

# Side effects, file/network access, sleeps, catalog/settings access, sequences.
_DENY_FUNCS = frozenset({
    "pg_sleep", "pg_sleep_for", "pg_sleep_until", "sleep", "benchmark", "waitfor", "pg_read_file",
    "pg_read_binary_file", "pg_ls_dir", "pg_stat_file", "lo_import", "lo_export", "lo_get", "dblink",
    "dblink_exec", "load_extension", "readfile", "writefile", "edit", "read_csv", "read_csv_auto",
    "read_parquet", "parquet_scan", "read_json", "read_json_auto", "read_ndjson", "read_text", "read_blob",
    "read_xlsx", "glob", "sniff_csv", "query", "query_table", "getenv", "current_setting", "set_config",
    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf", "nextval", "setval", "load_file",
    "sys_exec", "sys_eval", "xp_cmdshell", "openrowset", "opendatasource", "openquery", "http_get",
    "http_post", "httpfs", "copy_to", "txid_current", "pg_advisory_lock", "pg_notify", "sqlite_version",
    "duckdb_settings", "duckdb_secrets", "duckdb_extensions", "pragma_table_info", "system",
})
_ALLOWED_TABLE_FUNCS = frozenset({"unnest", "generate_series", "range"})
_BOUNDING = (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ, exp.Between)


@dataclass
class Validation:
    ok: bool
    sql: str | None = None  # executable SQL (rendered from the validated AST); None when rejected
    code: str = ""  # machine-readable rejection code
    reason: str = ""  # human-readable rejection reason
    tables: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    pii_filtered: bool = False  # query reads a PII column (filter/COUNT): small-count rule applies to its result
    count_columns: list[int] = field(default_factory=list)  # output positions holding COUNTs (bucketed below 5)

    @classmethod
    def reject(cls, code: str, reason: str, **kw) -> "Validation":
        return cls(ok=False, code=code, reason=reason, **kw)


class SqlValidator:
    """AST validation for one source (or one federation). Rejects anything but a single read-only query
    over allowlisted tables; enforces the PII-output and large-table rules."""

    def __init__(self, dialect: str, catalog: Catalog, *, scan_threshold_rows: int = 500_000,
                 estimate_rows: Callable[[TableMeta], int] | None = None, as_of_hint: Callable[[], str] | None = None,
                 sqlas: "SqlasGuard | None" = None):
        self.dialect = dialect
        self.catalog = catalog
        self.scan_threshold_rows = scan_threshold_rows
        self.estimate_rows = estimate_rows or (lambda meta: 0)
        self.as_of_hint = as_of_hint or (lambda: "")
        self.sqlas = sqlas

    def validate(self, sql: str, *, allow_pii_output: bool = False) -> Validation:
        if not sql or not sql.strip():
            return Validation.reject("empty", "No SQL given.")
        try:
            statements = [s for s in sqlglot.parse(sql, read=self.dialect) if s is not None]
        except ParseError as e:
            return Validation.reject("parse_error", f"SQL could not be parsed ({self.dialect}): {str(e).splitlines()[0][:200]}")
        if len(statements) != 1:
            return Validation.reject("multi_statement", f"Exactly one statement is allowed; got {len(statements)}.")
        tree = statements[0]
        if not isinstance(tree, exp.Query) or isinstance(tree, exp.Subquery) and not isinstance(tree.unnest(), exp.Query):
            return Validation.reject("not_select", f"Only SELECT / WITH … SELECT is allowed; got {type(tree).__name__.upper()}.")

        for node in tree.walk():
            if isinstance(node, _FORBIDDEN):
                return Validation.reject("forbidden_node", f"{type(node).__name__.upper()} is not allowed (read-only access).")
            if isinstance(node, exp.Func):
                fname = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
                if fname in _DENY_FUNCS:
                    return Validation.reject("forbidden_function", f"Function {fname}() is not allowed.")

        cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        used: dict[tuple[str | None, str], TableMeta] = {}
        for t in tree.find_all(exp.Table):
            if not isinstance(t.this, exp.Identifier):
                fn = t.this.name.lower() if isinstance(t.this, exp.Anonymous) else type(t.this).__name__.lower()
                if fn not in _ALLOWED_TABLE_FUNCS:
                    return Validation.reject("forbidden_function", f"Table function {fn}() is not allowed.")
                continue
            if not t.db and not t.catalog and t.name.lower() in cte_names:
                continue
            meta = self.catalog.resolve(t)
            if meta is None:
                allowed = ", ".join(sorted(f"{q}.{m.name}" if q else m.name for (q, _), m in self.catalog.tables.items()))
                return Validation.reject("table_not_allowed", f"Table {t.sql()} is not available. Allowed: {allowed}.")
            used[(meta.qualifier, meta.name.lower())] = meta
            if not t.db and not t.catalog or t.db.lower() in self.catalog.default_schemas:
                t.set("catalog", None)
                t.set("db", exp.to_identifier(meta.qualifier) if meta.qualifier else None)

        warnings: list[str] = []
        try:
            analysed = qualify(tree.copy(), schema=self.catalog.sqlglot_schema(), dialect=self.dialect,
                               validate_qualify_columns=False, quote_identifiers=False)
            root = build_scope(analysed)
        except (OptimizeError, Exception) as e:  # noqa: BLE001 — any analysis failure fails closed below
            analysed, root = None, None
            warnings.append(f"lineage analysis failed: {str(e)[:120]}")

        # PII output rule (decision Q4): trace each output column's lineage; COUNT over PII is fine.
        if not allow_pii_output and any(m.pii for m in used.values()):
            if root is None:
                return Validation.reject("pii_unverifiable", "Query touches PII columns and its column lineage could not be traced.", warnings=warnings)
            leaks = _leaking_outputs(root, self.catalog)
            if leaks:
                cols = ", ".join(sorted({f"{t}.{c}" for _, src in leaks for t, c in src}))
                return Validation.reject("pii_output", f"Output column(s) {', '.join(n for n, _ in leaks)} expose PII ({cols}). "
                                         "You may filter on or COUNT these columns, but not return them.", warnings=warnings)

        # Small-count rule: when PII is only filtered on / counted, restrict the shape and bucket counts < 5.
        pii_filtered, count_columns = False, []
        if not allow_pii_output and any(m.pii for m in used.values()):
            refs = _pii_refs(analysed, self.catalog) if root is not None else None
            if refs is None:
                return Validation.reject("pii_unverifiable", "Query touches PII columns and could not be analysed.", warnings=warnings)
            if refs:
                problem, count_columns = _small_count_shape(analysed, refs)
                if problem:
                    return Validation.reject("pii_filter_shape", problem, warnings=warnings)
                pii_filtered = True

        # Large-table rule: a big table needs a bounding filter on its time column.
        for meta in used.values():
            if not (meta.large and meta.time_column):
                continue
            if root is not None and _time_bounded(analysed, meta):
                continue
            est = self.estimate_rows(meta)
            if est > self.scan_threshold_rows:
                hint = self.as_of_hint()
                return Validation.reject(
                    "unbounded_large_scan",
                    f"Table {meta.name} is large (~{est:,} rows by EXPLAIN estimate). Add a date range on "
                    f"{meta.name}.{meta.time_column} (e.g. {meta.time_column} BETWEEN '…' AND '…')"
                    + (f"; data runs up to {hint}." if hint else "."), warnings=warnings)

        rendered = render(tree, self.dialect)
        tables = sorted({f"{q}.{n}" if q else n for (q, n) in used})
        if self.sqlas is not None:
            blocked, w = self.sqlas.check_sql(rendered, self.catalog, used)
            warnings += w
            if blocked:
                return Validation.reject("sqlas_blocked", blocked, tables=tables, warnings=warnings)
        return Validation(ok=True, sql=rendered, tables=tables, warnings=warnings, pii_filtered=pii_filtered,
                          count_columns=count_columns)


def repair_reserved_tables(sql: str, names: list[str]) -> str:
    """Quote bare reserved table names like `FROM order` / `JOIN order` / `order.amount` — LLMs often forget."""
    for n in names:
        sql = re.sub(rf'(?i)\b(from|join)\s+{n}\b(?!\s+by\b)', rf'\1 "{n}"', sql)
        sql = re.sub(rf'(?i)(?<!["\w.]){n}\.(\w+)', rf'"{n}".\1', sql)
    return sql


# --------------------------------------------------------------------------------------------- lineage

def _leaking_outputs(root: Scope, catalog: Catalog) -> list[tuple[str, set[tuple[str, str]]]]:
    return [(name, t) for name, t in _scope_outputs(root, catalog, {}) if t]


def _scope_outputs(scope: Scope, catalog: Catalog, memo: dict) -> list[tuple[str, set]]:
    if id(scope) in memo:
        return memo[id(scope)]
    memo[id(scope)] = []  # recursion guard
    if isinstance(scope.expression, exp.SetOperation):  # sqlglot: is_set_operation is False on the root
        left, right = (_scope_outputs(s, catalog, memo) for s in scope.set_operation_scopes)
        out = [(ln, lt | rt) for (ln, lt), (_, rt) in zip(left, right)]
    else:
        out = [(sel.alias_or_name, _taint(sel, scope, catalog, memo)) for sel in scope.expression.selects]
    memo[id(scope)] = out
    return out


def _taint(node: exp.Expression, scope: Scope, catalog: Catalog, memo: dict) -> set[tuple[str, str]]:
    """PII (table, column) pairs whose *values* can reach this expression's output."""
    if isinstance(node, (exp.Count, exp.Predicate)):  # counts and booleans don't carry the value
        return set()
    if isinstance(node, exp.Column):
        src = scope.sources.get(node.table)
        if isinstance(src, exp.Table):
            meta = catalog.resolve(src)
            return {(meta.name.lower(), node.name.lower())} if meta and node.name.lower() in meta.pii else set()
        if isinstance(src, Scope):
            return set().union(*(t for n, t in _scope_outputs(src, catalog, memo) if n.lower() == node.name.lower()))
        # Unresolved: assume the worst if any source in scope has a PII column with this name.
        return {(m.name.lower(), node.name.lower()) for s in scope.sources.values() if isinstance(s, exp.Table)
                for m in [catalog.resolve(s)] if m and node.name.lower() in m.pii}
    if isinstance(node, exp.Subquery) or isinstance(node, exp.Query) and node is not scope.expression:
        inner = node.unnest() if isinstance(node, exp.Subquery) else node
        for sub in scope.subquery_scopes + scope.derived_table_scopes + scope.set_operation_scopes:
            if sub.expression is inner:
                return set().union(set(), *(t for _, t in _scope_outputs(sub, catalog, memo)))
        return set()
    if isinstance(node, exp.Case):
        parts = [i.args.get("true") for i in node.args.get("ifs", [])] + [node.args.get("default")]
        return set().union(set(), *(_taint(p, scope, catalog, memo) for p in parts if p is not None))
    if isinstance(node, exp.If):
        return set().union(set(), *(_taint(node.args[k], scope, catalog, memo) for k in ("true", "false") if node.args.get(k)))
    if isinstance(node, exp.Window):
        return _taint(node.this, scope, catalog, memo)
    return set().union(set(), *(_taint(c, scope, catalog, memo) for c in node.iter_expressions()))


SMALL_COUNT_MIN = 5
_BAD_ANCESTORS = (exp.Or, exp.Not, exp.Xor, exp.Case, exp.If)


def _pii_refs(tree: exp.Expression, catalog: Catalog) -> list[exp.Column]:
    """Every column node that reads a PII column of a base table (resolved per scope)."""
    out = []
    for scope in traverse_scope(tree):
        for col in scope.columns:
            src = scope.sources.get(col.table)
            if isinstance(src, exp.Table):
                meta = catalog.resolve(src)
                if meta and col.name.lower() in meta.pii:
                    out.append(col)
    return out


def _small_count_shape(tree: exp.Expression, refs: list[exp.Column]) -> tuple[str, list[int]]:
    """PII may appear only inside COUNT(...) or in AND-only WHERE/HAVING/JOIN-ON conditions (no OR/NOT/CASE anywhere
    above it); the query may then output only COUNTs and non-PII GROUP BY keys. Returns (problem, count positions)."""
    for col in refs:
        place, node = None, col
        while node.parent is not None:
            parent = node.parent
            if isinstance(parent, _BAD_ANCESTORS):
                return (f"{col.sql()} is used under {type(parent).__name__.upper()}; when filtering on personal data only "
                        "AND-combined conditions are allowed (prevents probing one person with OR).", [])
            if place is None:
                if isinstance(parent, exp.Count):
                    place = "count"
                elif isinstance(parent, (exp.Where, exp.Having)) or (isinstance(parent, exp.Join) and node is parent.args.get("on")):
                    place = "filter"
                elif isinstance(parent, (exp.Select, exp.Group, exp.Order, exp.Ordered, exp.Window)):
                    return (f"{col.sql()} may only be filtered on (WHERE) or counted, not used in the select list, "
                            "GROUP BY, ORDER BY or a window.", [])
            node = parent
    if isinstance(tree, exp.SetOperation):
        return ("UNION/EXCEPT/INTERSECT is not allowed when filtering on personal data.", [])
    group = {g.sql() for g in (tree.args.get("group").expressions if tree.args.get("group") else [])}
    counts = []
    for i, sel in enumerate(tree.selects):
        inner = sel.unalias()
        if isinstance(inner, exp.Count):
            counts.append(i)
        elif inner.sql() not in group:
            return (f"Output {sel.alias_or_name!r}: when filtering on personal data the result may only contain "
                    "COUNT(...) and GROUP BY columns (small counts are shown as '<5').", [])
    return "", counts


def bucket_small_counts(rows: list[list], count_columns: list[int]) -> list[list]:
    """Counts below SMALL_COUNT_MIN become '<5' (k-anonymity floor for PII-filtered results)."""
    out = []
    for r in rows:
        r = list(r)
        for i in count_columns:
            if isinstance(r[i], (int, float)) and r[i] < SMALL_COUNT_MIN:
                r[i] = f"<{SMALL_COUNT_MIN}"
        out.append(r)
    return out


def _time_bounded(tree: exp.Expression, meta: TableMeta) -> bool:
    """True if every scope that reads *meta* bounds it with a comparison on its time column."""
    reads = False
    for scope in traverse_scope(tree):
        aliases = [a for a, s in scope.sources.items() if isinstance(s, exp.Table) and s.name.lower() == meta.name.lower()
                   and (s.db or "").lower() == (meta.qualifier or "").lower()]
        if not aliases:
            continue
        reads = True
        sel = scope.expression
        conds = [sel.args.get("where")] + [j.args.get("on") for j in sel.args.get("joins") or []]
        bounded = any(
            isinstance(cmp, _BOUNDING) and any(c.table in aliases and c.name.lower() == meta.time_column.lower()
                                               for c in cmp.find_all(exp.Column))
            for cond in conds if cond is not None for cmp in cond.find_all(*_BOUNDING))
        if not bounded:
            return False
    return reads


# --------------------------------------------------------------------------------------------- injection / PII text

_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?\b")

# Text in DATABASE VALUES that tries to instruct a model (indirect prompt injection). Our own patterns; enforcement
# must not depend on SQLAS (advisory only).
_INJECTION = re.compile(
    r"(ignore|disregard|forget|override)\s+(all\s+|any\s+|the\s+|your\s+|previous\s+|prior\s+|above\s+)*"
    r"(instructions?|rules?|prompts?|guidelines|context)"
    r"|system\s+prompt|you\s+are\s+now|act\s+as\b|new\s+instructions?|developer\s+mode|jailbreak"
    r"|<\s*/?\s*(system|assistant|user|instruction)\s*>|\[/?(INST|SYS)\]|<\|im_(start|end)\|>"
    r"|\b(route|respond|answer|reply)\s+(to|with|as)\s+(unsupported|analytics|lookup)\b"
    r"|https?://|www\.", re.I)
_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\d\s().-]{6,}\d)(?!\d)")
_EMAIL = re.compile(r"\b[\w.%+-]+@[\w.-]+\.[a-z]{2,}\b", re.I)


def looks_like_injection(text) -> bool:
    return isinstance(text, str) and bool(_INJECTION.search(text))


def unmasked_contact(text: str) -> str | None:
    """Our own output PII check: an e-mail address or a phone-like run of >= 10 digits (ISO dates excluded; 8-digit
    BIRD account numbers are not phones)."""
    t = _ISO_DATE.sub(" ", str(text))
    if _EMAIL.search(t):
        return "email"
    for m in _PHONE.finditer(t):
        if sum(ch.isdigit() for ch in m.group()) >= 10:
            return "phone"
    return None


# --------------------------------------------------------------------------------------------- SQLAS

_BLOCKING_INPUT = ("PROMPT_INJECTION:", "DANGEROUS_INPUT: malicious_sql_intent",
                   "DANGEROUS_INPUT: credential_extraction", "DANGEROUS_INPUT: bypass_attempt")


def _set_operation_leaves(node: exp.Expression) -> list[exp.Expression]:
    if isinstance(node, exp.SetOperation):
        return _set_operation_leaves(node.this) + _set_operation_leaves(node.expression)
    return [node]


@dataclass
class GuardResult:
    blocked: bool
    reason: str = ""
    warnings: list[str] = field(default_factory=list)


class SqlasGuard:
    """SQLAS GuardrailPipeline (sqlas==2.8.0) with BankQL's policies (SPEC.md section 10)."""

    def __init__(self, sql_threshold: float = 0.65, output_threshold: float = 0.85):
        from sqlas import GuardrailPipeline

        # A sentinel PII list: column-name PII matching is replaced by our lineage rule (Q4), and an empty
        # list would silently fall back to SQLAS's defaults.
        self.pipeline = GuardrailPipeline(pii_columns=["__bankql_lineage_enforced__"],
                                          sql_threshold=sql_threshold, output_threshold=output_threshold)

    def check_input(self, question: str) -> GuardResult:
        r = self.pipeline.check_input(question)
        issues = [i for i in r.issues if i != "none"]
        blocking = [i for i in issues if i.startswith(_BLOCKING_INPUT)]
        return GuardResult(bool(blocking), f"Input blocked: {blocking[0]}" if blocking else "",
                           [f"sqlas_input: {i}" for i in issues if i not in blocking])

    def check_sql(self, sql: str, catalog: Catalog, used: dict) -> tuple[str, list[str]]:
        """SQLAS check_sql on comment-free SQL. Its read_only_compliance only accepts SELECT/WITH roots, so
        UNION/EXCEPT/INTERSECT would always be 'not read-only': each SELECT branch is checked on its own."""
        valid_tables = {m.name.lower() for m in used.values()}
        valid_cols = {m.name.lower(): {c.lower() for c in m.columns} for m in used.values()}
        tree = sqlglot.parse_one(sql)
        parts = [sql] if not isinstance(tree, exp.SetOperation) else [
            s.sql(comments=False) for s in _set_operation_leaves(tree)]
        blocked, warnings = "", []
        for part in parts:
            r = self.pipeline.check_sql(part, valid_tables, valid_cols)
            warnings += [f"sqlas_sql: {i}" for i in r.issues if i != "none" and f"sqlas_sql: {i}" not in warnings]
            if r.blocked and not blocked:
                blocked = r.block_reason or "SQLAS blocked the SQL"
        return blocked, warnings

    def check_output(self, summary: str, pii_cells: list[list[str]]) -> GuardResult:
        """Q3: scan the summary (ISO dates removed — SQLAS's phone regex matches them) and only the cells of
        PII-flagged columns (SQLAS's row patterns would flag every date column)."""
        r = self.pipeline.check_output(_ISO_DATE.sub(" ", summary), {"rows": pii_cells})
        issues = [i for i in r.issues if i != "none"]
        # Only PII-column cells and date-free text are scanned, so any hit means masking failed: block on any
        # issue (SQLAS alone scores one row hit 0.9, above its 0.85 threshold, and lets it through).
        return GuardResult(bool(issues), r.block_reason or (f"Output blocked: {issues[0]}" if issues else ""),
                           [f"sqlas_output: {i}" for i in issues])
