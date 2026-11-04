"""Deterministic entity resolution — no LLM. IDs resolve by exact match; names by fuzzy match (rapidfuzz)
against the full client_profile table. Several matches -> ask the user to choose; none -> say so."""
from __future__ import annotations

import time
import unicodedata
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process

from bankql.connectors import Source

MIN_SCORE = 80  # candidates below this are not shown
ACCEPT_SCORE = 92  # a single fuzzy match must reach this ...
ACCEPT_GAP = 8  # ... and beat the runner-up by this much
MAX_CHOICES = 5
NAME_INDEX_MAX_ROWS = 200_000  # shortcut: in-memory index; above this use SQLite FTS5 / DuckDB / pg_trgm instead
NAME_INDEX_TTL_S = 600


@dataclass
class Resolution:
    status: str  # found | ambiguous | not_found | unsupported
    kind: str  # client | account
    query: str
    matches: list[dict] = field(default_factory=list)  # found: exactly one; ambiguous: the choices
    message: str = ""

    @property
    def id(self) -> int | None:
        return self.matches[0][f"{self.kind}_id"] if self.status == "found" else None


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return " ".join(s.lower().replace(".", " ").split())


class Resolver:
    def __init__(self, source: Source):
        self.source = source
        self._names: tuple[float, list[dict]] | None = None

    # ------------------------------------------------------------------------------------------ IDs

    def client_by_id(self, client_id: int) -> Resolution:
        return self._by_id("client", client_id)

    def account_by_id(self, account_id: int) -> Resolution:
        return self._by_id("account", account_id)

    def _by_id(self, kind: str, value: int) -> Resolution:
        try:
            ident = int(value)
        except (TypeError, ValueError):
            return Resolution("not_found", kind, str(value), message=f"'{value}' is not a valid {kind} id.")
        r = self.source.run(f"SELECT {kind}_id FROM {kind} WHERE {kind}_id = :id", {"id": ident}, max_rows=1)
        if not r.rows:
            return Resolution("not_found", kind, str(ident), message=f"No {kind} with id {ident} exists.")
        return Resolution("found", kind, str(ident), [{f"{kind}_id": ident}])

    # ------------------------------------------------------------------------------------------ names

    def _name_index(self) -> list[dict]:
        if self._names is None or time.monotonic() - self._names[0] > NAME_INDEX_TTL_S:
            r = self.source.run("SELECT client_id, full_name, city FROM client_profile ORDER BY client_id",
                                max_rows=NAME_INDEX_MAX_ROWS + 1, allow_pii_output=True)
            if r.truncated or len(r.rows) > NAME_INDEX_MAX_ROWS:
                raise RuntimeError("client_profile is too large for the in-memory name index; use FTS5/DuckDB/pg_trgm")
            self._names = (time.monotonic(), [{"client_id": cid, "full_name": n, "city": c, "_n": _norm(n)}
                                              for cid, n, c in r.rows])
        return self._names[1]

    def client_by_name(self, name: str, city: str | None = None) -> Resolution:
        if not self.source.has_table("client_profile"):
            return Resolution("unsupported", "client", name,
                              message=f"Source '{self.source.name}' has no client names (BIRD clients are anonymous); "
                                      "please give a client id.")
        people = self._name_index()
        if city:
            people = [p for p in people if fuzz.ratio(_norm(city), _norm(p["city"])) >= 85]
        q = _norm(name)
        if not q:
            return Resolution("not_found", "client", name, message="No name given.")
        exact = [p for p in people if p["_n"] == q]
        if exact:
            return self._pick(name, [dict(p, score=100.0) for p in exact])
        scored = process.extract(q, [p["_n"] for p in people], scorer=fuzz.WRatio, limit=MAX_CHOICES + 5,
                                 score_cutoff=MIN_SCORE)
        cands = [dict(people[i], score=round(s, 1)) for _, s, i in scored]
        if not cands:
            where = f" in {city}" if city else ""
            return Resolution("not_found", "client", name, message=f"No client named '{name}'{where} was found.")
        if cands[0]["score"] >= ACCEPT_SCORE and (len(cands) == 1 or cands[0]["score"] - cands[1]["score"] >= ACCEPT_GAP):
            return self._pick(name, cands[:1])
        return self._pick(name, cands)

    def _pick(self, name: str, cands: list[dict]) -> Resolution:
        clean = [{k: v for k, v in c.items() if k != "_n"} for c in cands[:MAX_CHOICES]]
        if len(clean) == 1:
            return Resolution("found", "client", name, clean)
        opts = "; ".join(f"{c['full_name']} ({c['city']}, client {c['client_id']})" for c in clean)
        return Resolution("ambiguous", "client", name, clean, message=f"Several clients match '{name}': {opts}. Which one?")
