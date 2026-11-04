"""Lookup tools: fixed, parameterized SQL — never LLM-written. Templates are written once with ANSI quoting
("order" is a reserved word) and rendered per dialect by sqlglot; every call still goes through the
validator, so a template that broke a rule would be refused like any other SQL."""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date

import sqlglot

from bankql.connectors import QueryResult, Source
from bankql.guardrails import render

DEFAULT_STATEMENT_MONTHS = 3

TEMPLATES = {
    "get_client": """
        SELECT c.client_id, c.gender, c.birth_date, c.district_id, d.A2 AS district_name, d.A3 AS region
        FROM client AS c JOIN district AS d ON d.district_id = c.district_id
        WHERE c.client_id = :client_id""",
    "get_client_profile": """
        SELECT p.full_name, p.city, p.phone_masked FROM client_profile AS p WHERE p.client_id = :client_id""",
    "get_accounts": """
        SELECT a.account_id, dp.type AS disposition, a.frequency, a.date AS opened, a.district_id
        FROM disp AS dp JOIN account AS a ON a.account_id = dp.account_id
        WHERE dp.client_id = :client_id ORDER BY a.account_id""",
    "get_statement": """
        SELECT t.trans_id, t.date, t.type, t.operation, t.amount, t.balance, t.k_symbol, t.bank, t.account
        FROM trans AS t
        WHERE t.account_id = :account_id AND t.date >= :date_from AND t.date <= :date_to
        ORDER BY t.date, t.trans_id""",
    "get_cards": """
        SELECT cd.card_id, cd.type, cd.issued, dp.account_id, dp.type AS disposition
        FROM disp AS dp JOIN card AS cd ON cd.disp_id = dp.disp_id
        WHERE dp.client_id = :client_id ORDER BY cd.card_id""",
    "get_loans_by_client": """
        SELECT l.loan_id, l.account_id, l.date, l.amount, l.duration, l.payments, l.status
        FROM disp AS dp JOIN loan AS l ON l.account_id = dp.account_id
        WHERE dp.client_id = :client_id AND dp.type = 'OWNER' ORDER BY l.loan_id""",
    "get_loans_by_account": """
        SELECT l.loan_id, l.account_id, l.date, l.amount, l.duration, l.payments, l.status
        FROM loan AS l WHERE l.account_id = :account_id ORDER BY l.loan_id""",
    "get_orders": """
        SELECT o.order_id, o.account_id, o.bank_to, o.account_to, o.amount, o.k_symbol
        FROM "order" AS o WHERE o.account_id = :account_id ORDER BY o.order_id""",
}


@dataclass
class ToolResult:
    tool: str
    sql: str
    params: dict
    columns: list[str]
    rows: list[list]
    truncated: bool = False
    notes: list[str] = field(default_factory=list)
    legend: dict = field(default_factory=dict)
    elapsed_ms: float = 0.0


class ToolError(ValueError):
    """Bad arguments (refuse with this message)."""


def template_sql(name: str, dialect: str) -> str:
    return render(sqlglot.parse_one(TEMPLATES[name], read="postgres"), dialect)


def _iso(v, what: str) -> date:
    try:
        return v if isinstance(v, date) else date.fromisoformat(str(v))
    except ValueError as e:
        raise ToolError(f"{what} must be a date like 1998-01-31, got {v!r}") from e


def _months_back(d: date, months: int) -> date:
    y, m = divmod(d.year * 12 + d.month - 1 - months, 12)
    return date(y, m + 1, min(d.day, calendar.monthrange(y, m + 1)[1]))


def _pos_int(v, what: str) -> int:
    try:
        i = int(v)
    except (TypeError, ValueError) as e:
        raise ToolError(f"{what} must be a whole number, got {v!r}") from e
    if i <= 0:
        raise ToolError(f"{what} must be positive, got {i}")
    return i


class LookupTools:
    def __init__(self, source: Source):
        self.source = source
        self._sql = {k: template_sql(k, source.dialect) for k in TEMPLATES}

    def _run(self, tool: str, key: str, params: dict, *, pii: bool = False, notes=None) -> ToolResult:
        r: QueryResult = self.source.run(self._sql[key], params, allow_pii_output=pii)
        return ToolResult(tool, r.sql, params, r.columns, r.rows, r.truncated, list(notes or []) + r.warnings,
                          self.source.semantic.legend(r.columns, r.tables), r.elapsed_ms)

    def get_client(self, client_id) -> ToolResult:
        cid = _pos_int(client_id, "client_id")
        res = self._run("get_client", "get_client", {"client_id": cid})
        if res.rows and self.source.has_table("client_profile"):
            # Q4: lookup tools may return the (synthetic) name and the masked phone.
            prof = self.source.run(self._sql["get_client_profile"], {"client_id": cid}, allow_pii_output=True)
            if prof.rows:
                res.columns += prof.columns
                res.rows = [res.rows[0] + prof.rows[0]]
                res.sql += ";\n" + prof.sql
        return res

    def get_accounts(self, client_id) -> ToolResult:
        return self._run("get_accounts", "get_accounts", {"client_id": _pos_int(client_id, "client_id")})

    def get_statement(self, account_id, date_from=None, date_to=None) -> ToolResult:
        aid = _pos_int(account_id, "account_id")
        notes = []
        end = _iso(date_to, "date_to") if date_to else self.source.as_of()
        start = _iso(date_from, "date_from") if date_from else _months_back(end, DEFAULT_STATEMENT_MONTHS)
        if not date_from and not date_to:
            notes.append(f"No dates given: showing the last {DEFAULT_STATEMENT_MONTHS} months up to "
                         f"{end.isoformat()} (data as of {self.source.as_of().isoformat()}).")
        if start > end:
            raise ToolError(f"date_from {start} is after date_to {end}")
        # SQLite stores dates as ISO text; ISO strings compare correctly there and bind as DATE elsewhere.
        bind = (lambda d: d.isoformat()) if self.source.dialect == "sqlite" else (lambda d: d)
        res = self._run("get_statement", "get_statement", {"account_id": aid, "date_from": bind(start), "date_to": bind(end)},
                        notes=notes)
        res.params = {"account_id": aid, "date_from": start.isoformat(), "date_to": end.isoformat()}
        return res

    def get_cards(self, client_id) -> ToolResult:
        return self._run("get_cards", "get_cards", {"client_id": _pos_int(client_id, "client_id")})

    def get_loans(self, client_id=None, account_id=None) -> ToolResult:
        if account_id is not None:
            return self._run("get_loans", "get_loans_by_account", {"account_id": _pos_int(account_id, "account_id")})
        if client_id is not None:
            return self._run("get_loans", "get_loans_by_client", {"client_id": _pos_int(client_id, "client_id")})
        raise ToolError("get_loans needs a client_id or an account_id")

    def get_orders(self, account_id) -> ToolResult:
        return self._run("get_orders", "get_orders", {"account_id": _pos_int(account_id, "account_id")})


TOOL_NAMES = ["get_client", "get_accounts", "get_statement", "get_cards", "get_loans", "get_orders"]
