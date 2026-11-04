"""PII scrubbing for text that leaves the request path: the audit log, session history sent back to the model.

Masks (1) names that occur in client_profile, (2) e-mail addresses / unmasked phone numbers, (3) in SQL, string
literals compared with a PII column (via the AST: `WHERE full_name = 'Rahul Sharma'` → `WHERE full_name = '***'`).
"""
from __future__ import annotations

import re
import time

import sqlglot
from sqlglot import exp

from bankql.guardrails import _EMAIL, _ISO_DATE, _PHONE

MASK = "[PII]"
_TTL_S = 600


class Scrubber:
    def __init__(self, source):
        self.source = source
        self._names: tuple[float, re.Pattern | None] | None = None

    def _name_re(self) -> re.Pattern | None:
        if self._names is None or time.monotonic() - self._names[0] > _TTL_S:
            pat = None
            if self.source.has_table("client_profile"):
                r = self.source.run("SELECT full_name FROM client_profile", max_rows=200_000, allow_pii_output=True)
                # shortcut: one alternation regex over every name; fine for ~10^5 names, use Aho-Corasick beyond that
                names = sorted({n for (n,) in r.rows if n and len(n) > 2}, key=len, reverse=True)
                if names:
                    pat = re.compile(r"\b(?:" + "|".join(re.escape(n) for n in names) + r")\b", re.I)
            self._names = (time.monotonic(), pat)
        return self._names[1]

    def text(self, s: str | None) -> str | None:
        if not s:
            return s
        pat = self._name_re()
        if pat is not None:
            s = pat.sub(MASK, s)
        s = _EMAIL.sub(MASK, s)
        dates = {m.group(): f"\x00{i}\x00" for i, m in enumerate(_ISO_DATE.finditer(s))}
        for d, tok in dates.items():
            s = s.replace(d, tok)
        s = _PHONE.sub(lambda m: MASK if sum(c.isdigit() for c in m.group()) >= 10 else m.group(), s)
        for d, tok in dates.items():
            s = s.replace(tok, d)
        return s

    def sql(self, sql: str | None) -> str | None:
        if not sql:
            return sql
        pii = {c for m in self.source.catalog.tables.values() for c in m.pii}
        try:
            parts = []
            for stmt in sqlglot.parse(sql, read=self.source.dialect):
                if stmt is None:
                    continue
                for col in list(stmt.find_all(exp.Column)):
                    if col.name.lower() not in pii:
                        continue
                    cond = col.find_ancestor(exp.Predicate, exp.In)
                    for lit in (cond.find_all(exp.Literal) if cond else []):
                        if lit.is_string:
                            lit.replace(exp.Literal.string("***"))
                parts.append(stmt.sql(dialect=self.source.dialect))
            return self.text(";\n".join(parts))
        except Exception:  # noqa: BLE001 — unparseable: fall back to text masking
            return self.text(sql)
