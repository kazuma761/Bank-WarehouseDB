"""Router (Gemma): JSON-schema-constrained intent + entity extraction, follow-up rewriting, summaries.
The router only extracts — SQL for lookups lives in bankql.tools. Extracted IDs/years must literally appear
in the question (the LLM never invents or retypes numbers)."""
from __future__ import annotations

import calendar
import json
import re
from dataclasses import dataclass, field
from datetime import date

from bankql.llm import LLMClient, field_probability
from bankql.tools import TOOL_NAMES

PERIODS = ["last_month", "this_month", "last_3_months", "last_6_months", "last_12_months", "this_year", "last_year"]

ROUTER_SCHEMA = {
    "type": "object",
    "properties": {  # "intent" first so its tokens' logprobs measure the routing decision
        "intent": {"type": "string", "enum": ["lookup", "analytics", "unsupported"]},
        "tools": {"type": "array", "maxItems": 3, "items": {
            "type": "object",
            "properties": {
                "tool": {"type": "string", "enum": TOOL_NAMES},
                "client_id": {"type": ["integer", "null"]},
                "account_id": {"type": ["integer", "null"]},
                "client_name": {"type": ["string", "null"]},
                "city": {"type": ["string", "null"]},
                "date_from": {"type": ["string", "null"]},
                "date_to": {"type": ["string", "null"]},
                "period": {"type": ["string", "null"], "enum": PERIODS + [None]},
            },
            "required": ["tool", "client_id", "account_id", "client_name", "city", "date_from", "date_to", "period"],
        }},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string", "maxLength": 200},
    },
    "required": ["intent", "tools", "confidence", "reason"],
}

ROUTER_SYSTEM = """You route questions for a bank's read-only data assistant. Output JSON only.

intent:
- "lookup": facts about ONE specific client or account, answered by these tools:
  get_client(client_id | client_name)            profile of one client
  get_accounts(client_id | client_name)          accounts a client owns or may operate
  get_statement(account_id | client_id | client_name, dates)  transactions of one account
  get_cards(client_id | client_name)             cards of a client
  get_loans(client_id | client_name | account_id) loans
  get_orders(account_id | client_id | client_name) standing (permanent) payment orders
  A question may need up to 3 tools (e.g. "client 12's accounts and last month's statement").
- "analytics": counts, totals, averages, rankings, comparisons or lists across many clients/accounts/districts.
- "unsupported": anything else (chit-chat, advice, requests to change data, other topics).

Rules:
- Copy IDs exactly as written in the question. Never guess an ID. "customer"/"client" number -> client_id;
  "account"/"a/c" number -> account_id. A person's name -> client_name (and city if one is given).
- Dates: copy explicit dates as YYYY-MM-DD into date_from/date_to ("in 1997" -> 1997-01-01 .. 1997-12-31).
  Relative periods ("last month", "last 3 months", "this year") -> period, and leave the dates null.
- Questions may be formal, casual, misspelled, or Hinglish ("mera account dikhao", "client 12 ke loans").
- confidence: your probability (0-1) that intent and arguments are right. reason: a few words."""

REWRITE_SCHEMA = {
    "type": "object",
    "properties": {"is_follow_up": {"type": "boolean"}, "standalone_question": {"type": "string", "maxLength": 500}},
    "required": ["is_follow_up", "standalone_question"],
}
REWRITE_SYSTEM = """Rewrite the user's latest message into a standalone question using the conversation so far.
If it already stands alone, return it unchanged with is_follow_up=false. Keep every number and name exactly as
written in the conversation; do not answer the question. Output JSON only."""

SUMMARY_SCHEMA = {"type": "object", "properties": {"summary": {"type": "string", "maxLength": 400}}, "required": ["summary"]}
SUMMARY_SYSTEM = """Answer the question in 1-2 plain sentences using ONLY the result rows given. Every number you
write must appear in the rows or the row count. Do not mention SQL, tables or columns. Output JSON only."""


@dataclass
class ToolCall:
    tool: str
    client_id: int | None = None
    account_id: int | None = None
    client_name: str | None = None
    city: str | None = None
    date_from: str | None = None
    date_to: str | None = None
    period: str | None = None

    def args(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k != "tool" and v is not None}


@dataclass
class RouterDecision:
    intent: str
    tools: list[ToolCall] = field(default_factory=list)
    confidence: float = 0.0
    self_confidence: float | None = None
    logprob_confidence: float | None = None
    reason: str = ""
    warnings: list[str] = field(default_factory=list)
    raw: str = ""


def _digits_in(text: str) -> set[str]:
    return set(re.findall(r"\d+", text))


class Router:
    def __init__(self, llm: LLMClient, *, use_logprobs: bool = True):
        self.llm, self.use_logprobs = llm, use_logprobs

    def route(self, question: str) -> RouterDecision:
        r = self.llm.chat([{"role": "system", "content": ROUTER_SYSTEM}, {"role": "user", "content": question}],
                          temperature=0.0, max_tokens=400, json_schema=ROUTER_SCHEMA, logprobs=self.use_logprobs,
                          stage="router")
        try:
            data = json.loads(r.text)
            intent = data["intent"]
            assert intent in ("lookup", "analytics", "unsupported")
        except (ValueError, KeyError, TypeError, AssertionError):
            return RouterDecision("unparseable", confidence=0.0, reason="router output was not valid JSON", raw=r.text)
        self_conf = float(data.get("confidence") or 0.0)
        lp = field_probability(r.text, r.logprobs, "intent")
        d = RouterDecision(intent, confidence=lp if lp is not None else self_conf, self_confidence=self_conf,
                           logprob_confidence=lp, reason=str(data.get("reason", ""))[:200], raw=r.text)
        seen = _digits_in(question)
        for t in data.get("tools") or []:
            if t.get("tool") not in TOOL_NAMES:
                continue
            call = ToolCall(**{k: t.get(k) for k in ToolCall.__dataclass_fields__})
            for k in ("client_id", "account_id"):  # grounding: IDs must be written in the question
                v = getattr(call, k)
                if v is not None and str(v) not in seen:
                    d.warnings.append(f"router_ungrounded_{k}: {v}")
                    d.confidence = 0.0
            for k in ("date_from", "date_to"):
                v = getattr(call, k)
                if v is not None and not (re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(v)) and str(v)[:4] in seen):
                    d.warnings.append(f"router_ungrounded_{k}: {v}")
                    setattr(call, k, None)
                    d.confidence = min(d.confidence, 0.5)
            d.tools.append(call)
        if intent == "lookup" and not d.tools:
            d.confidence, d.reason = 0.0, "lookup without a tool"
        return d

    def rewrite(self, question: str, history: list[dict]) -> tuple[str, list[str]]:
        """talkdb-style follow-up rewriting. Returns (standalone question, warnings). Never touches past SQL."""
        convo = "\n".join(f"{'User' if h['role'] == 'user' else 'Assistant'}: {h['content']}" for h in history)
        r = self.llm.chat([{"role": "system", "content": REWRITE_SYSTEM},
                           {"role": "user", "content": f"Conversation:\n{convo}\n\nLatest message: {question}"}],
                          temperature=0.0, max_tokens=300, json_schema=REWRITE_SCHEMA, stage="rewrite")
        try:
            data = json.loads(r.text)
            out = str(data["standalone_question"]).strip()
        except (ValueError, KeyError, TypeError):
            return question, ["rewrite_unparseable: used the question as asked"]
        allowed = _digits_in(question + " " + convo)
        if not out or not _digits_in(out) <= allowed:
            return question, ["rewrite_ungrounded_number: used the question as asked"]
        return out, []

    def summarize(self, question: str, columns: list[str], rows: list[list], row_count: int) -> str | None:
        payload = json.dumps({"question": question, "row_count": row_count, "columns": columns, "rows": rows}, default=str)
        r = self.llm.chat([{"role": "system", "content": SUMMARY_SYSTEM}, {"role": "user", "content": payload}],
                          temperature=0.0, max_tokens=200, json_schema=SUMMARY_SCHEMA, stage="summary")
        try:
            return str(json.loads(r.text)["summary"]).strip() or None
        except (ValueError, KeyError, TypeError):
            return None


# ------------------------------------------------------------------------------------------- dates

def resolve_period(period: str | None, as_of: date) -> tuple[date, date] | None:
    """Relative periods count back from the source's as_of date (decision Q6)."""
    if not period:
        return None
    if period == "this_month":
        return as_of.replace(day=1), as_of
    if period == "last_month":
        first = as_of.replace(day=1)
        prev_last = date.fromordinal(first.toordinal() - 1)
        return prev_last.replace(day=1), prev_last
    if period in ("last_3_months", "last_6_months", "last_12_months"):
        n = int(period.split("_")[1])
        y, m = divmod(as_of.year * 12 + as_of.month - 1 - n, 12)
        return date(y, m + 1, min(as_of.day, calendar.monthrange(y, m + 1)[1])), as_of
    if period == "this_year":
        return date(as_of.year, 1, 1), as_of
    if period == "last_year":
        return date(as_of.year - 1, 1, 1), date(as_of.year - 1, 12, 31)
    return None


# ------------------------------------------------------------------------------------------- grounding

_NUM = re.compile(r"(?<![\w.])-?\d[\d,]*(?:\.\d+)?(?![\w])")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def ungrounded_numbers(summary: str, rows: list[list], extra: list = ()) -> list[str]:
    """Numbers (and ISO dates) in *summary* that appear neither in the result cells nor in *extra*."""
    cells = [v for r in rows for v in r] + list(extra)
    dates = {str(v)[:10] for v in cells if v is not None}
    nums: set[float] = set()
    for v in cells:
        try:
            nums.add(round(float(str(v).replace(",", "")), 2))
        except (TypeError, ValueError):
            nums.update(round(float(x.replace(",", "")), 2) for x in _NUM.findall(str(v)))
    bad = [d for d in _DATE.findall(summary) if d not in dates]
    for tok in _NUM.findall(_DATE.sub(" ", summary)):
        try:
            val = round(float(tok.replace(",", "")), 2)
        except ValueError:
            continue
        if val not in nums:
            bad.append(tok)
    return bad
