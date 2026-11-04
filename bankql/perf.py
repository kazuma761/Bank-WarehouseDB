"""Per-request latency spans and LLM call records (a contextvar, so nested code can record without plumbing).

    rec, token = perf.start()            # Agent.ask
    with perf.span("validation"): ...      # anywhere below, same request
    perf.stop(token)
    rec.spans  -> {"validation": [1.2, 0.8], "db_execute": [...], ...}   (ms, every occurrence)
    rec.llm    -> [{"stage", "prompt_tokens", "completion_tokens", "cached_tokens", "prefill_ms", "decode_ms", ...}]
"""
from __future__ import annotations

import contextvars
import time
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class Record:
    spans: dict[str, list[float]] = field(default_factory=dict)
    llm: list[dict] = field(default_factory=list)

    def add(self, name: str, ms: float) -> None:
        self.spans.setdefault(name, []).append(round(ms, 3))

    def totals(self) -> dict[str, float]:
        return {k: round(sum(v), 2) for k, v in self.spans.items()}


_current: contextvars.ContextVar[Record | None] = contextvars.ContextVar("bankql_perf", default=None)


@contextmanager
def span(name: str):
    rec = _current.get()
    t0 = time.perf_counter()
    try:
        yield
    finally:
        if rec is not None:
            rec.add(name, (time.perf_counter() - t0) * 1000)


def llm_call(stage: str, wall_ms: float, data: dict | None, prompt_chars: int | None = None) -> dict:
    """Record one LLM call from an OpenAI-compatible llama-server response (`usage` + llama.cpp `timings`)."""
    data = data or {}
    usage, t = data.get("usage") or {}, data.get("timings") or {}
    entry = {
        "stage": stage,
        "prompt_tokens": usage.get("prompt_tokens", t.get("prompt_n")),
        "completion_tokens": usage.get("completion_tokens", t.get("predicted_n")),
        "cached_tokens": t.get("cache_n", (usage.get("prompt_tokens_details") or {}).get("cached_tokens")),
        "prefill_ms": t.get("prompt_ms"),
        "decode_ms": t.get("predicted_ms"),
        "decode_tok_s": t.get("predicted_per_second"),
        "wall_ms": round(wall_ms, 1),
        "prompt_chars": prompt_chars,
    }
    rec = _current.get()
    if rec is not None:
        rec.llm.append(entry)
        rec.add("llm", wall_ms)
    return entry


def start() -> tuple[Record, contextvars.Token]:
    rec = Record()
    return rec, _current.set(rec)


def stop(token: contextvars.Token) -> None:
    _current.reset(token)
