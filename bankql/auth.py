"""API keys → principals with a policy (source allowlist, run_sql, rate limits).

Keys come from BANKQL_API_KEYS (default policy) and/or config/api_keys.yaml, which stores only SHA-256 hashes:

    defaults: {sources: ["*"], run_sql: false, asks_per_minute: 60, lookups_per_minute: 30}
    keys:
      - id: core-banking-app
        sha256: 3f1c…                 # `bankql hash-key <key>` prints it; the key itself lives in your secret store
        sources: [bank_pg]
        run_sql: false
        lookups_per_minute: 120
"""
from __future__ import annotations

import hashlib
import hmac
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import yaml


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


@dataclass(frozen=True)
class Policy:
    sources: tuple[str, ...] = ("*",)
    run_sql: bool = False
    asks_per_minute: int = 60
    lookups_per_minute: int = 30

    def allows_source(self, name: str) -> bool:
        return "*" in self.sources or name in self.sources


@dataclass(frozen=True)
class Principal:
    key_id: str  # never the key itself; safe to log
    policy: Policy = field(default_factory=Policy)


LIBRARY = Principal("library")  # in-process use: default policy


class Forbidden(Exception):
    """403: the key may not use this source / operation."""


class RateLimited(Exception):
    """429: the key exceeded a per-minute limit."""

    def __init__(self, kind: str, retry_after_s: int):
        super().__init__(f"rate limit exceeded for {kind}; retry after {retry_after_s}s")
        self.kind, self.retry_after_s = kind, retry_after_s


class KeyStore:
    def __init__(self, config_dir: str | Path | None = None):
        doc = {}
        path = Path(config_dir) / "api_keys.yaml" if config_dir else None
        if path and path.exists():
            doc = yaml.safe_load(path.read_text()) or {}
        d = doc.get("defaults") or {}
        self.default = Policy(**{**d, "sources": tuple(d.get("sources", ["*"]))})
        self._by_hash: dict[str, Principal] = {}
        for k in doc.get("keys") or []:
            pol = {key: v for key, v in {**d, **k}.items() if key in Policy.__dataclass_fields__}
            pol["sources"] = tuple(pol.get("sources", ["*"]))
            self._by_hash[k["sha256"].lower()] = Principal(k["id"], Policy(**pol))
        for key in (k.strip() for k in os.environ.get("BANKQL_API_KEYS", "").split(",")):
            if key and hash_key(key) not in self._by_hash:
                self._by_hash[hash_key(key)] = Principal(f"env-{hash_key(key)[:8]}", self.default)

    def authenticate(self, key: str | None) -> Principal | None:
        """Constant-time match against every configured hash (no early exit)."""
        h = hash_key(key or "")
        found = None
        for stored, principal in self._by_hash.items():
            if hmac.compare_digest(h, stored) and key:
                found = principal
        return found


class RateLimiter:
    """Sliding one-minute window per (key_id, kind). shortcut: in-process memory; use Redis for several workers."""

    def __init__(self):
        self._hits: dict[tuple[str, str], deque] = {}
        self._lock = threading.Lock()

    def hit(self, principal: Principal, kind: str, n: int = 1) -> None:
        limit = getattr(principal.policy, f"{kind}s_per_minute", None)
        if not limit or principal.key_id == "library":
            return
        now = time.monotonic()
        with self._lock:
            q = self._hits.setdefault((principal.key_id, kind), deque())
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) + n > limit:
                raise RateLimited(kind, max(1, int(60 - (now - q[0])) + 1))
            q.extend([now] * n)
