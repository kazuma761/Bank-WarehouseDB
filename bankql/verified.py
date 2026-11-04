"""Verified-query cache (talkdb's verified-query idea): normalised standalone question + source → the analytics SQL
that last answered it successfully. A hit skips the router and SQL generation; the SQL is still re-validated and
re-run against current data on every use.

Evaluation must not use this cache (hard rule 5): eval/run_*.py disable it."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import unicodedata
from pathlib import Path


def normalize(question: str) -> str:
    q = unicodedata.normalize("NFKC", question).lower()
    q = re.sub(r"[^\w\s]", " ", q)
    return " ".join(q.split())


class VerifiedQueries:
    def __init__(self, path: str | Path | None, max_entries: int = 5000):
        self.path = Path(path) if path else None
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._d: dict[str, dict] = {}
        if self.path and self.path.exists():
            try:
                self._d = json.loads(self.path.read_text())
            except ValueError:
                self._d = {}

    @staticmethod
    def key(source: str, schema_version: str, question: str) -> str:
        return f"{source}|{schema_version}|{normalize(question)}"

    def get(self, key: str) -> str | None:
        with self._lock:
            e = self._d.get(key)
            if e:
                e["hits"] = e.get("hits", 0) + 1
            return e["sql"] if e else None

    def put(self, key: str, sql: str) -> None:
        with self._lock:
            self._d[key] = {"sql": sql, "saved": time.time(), "hits": self._d.get(key, {}).get("hits", 0)}
            if len(self._d) > self.max_entries:  # drop the least-used
                for k in sorted(self._d, key=lambda k: self._d[k].get("hits", 0))[: len(self._d) - self.max_entries]:
                    self._d.pop(k)
            if self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._d))
                os.chmod(tmp, 0o600)
                tmp.replace(self.path)

    def forget(self, key: str) -> None:
        with self._lock:
            self._d.pop(key, None)
