"""LLM access: one OpenAI-compatible HTTP client for local llama.cpp servers, plus a deterministic FakeLLM for
tests. No cloud SDK is imported anywhere in this package."""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

import httpx


@dataclass
class LLMResponse:
    text: str
    logprobs: list[dict] | None = None  # [{"token": str, "logprob": float}, ...] when the server returns them
    latency_ms: float = 0.0


class LLMClient(Protocol):
    def chat(self, messages: list[dict], *, temperature: float = 0.0, max_tokens: int = 512,
             json_schema: dict | None = None, logprobs: bool = False) -> LLMResponse: ...


class OpenAICompatClient:
    """`POST {base_url}/chat/completions` against llama-server or llama-cpp-python's server.

    Structured output: `response_format={"type":"json_schema",...}` (OpenAI style, newer llama-server) or the
    top-level `json_schema` field (llama-server's native form). `structured_output="auto"` tries the first and
    falls back to the second on a 4xx, then remembers what worked."""

    def __init__(self, base_url: str, model: str = "local", timeout_s: float = 120, structured_output: str = "auto",
                 api_key: str | None = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.mode = structured_output
        self._http = httpx.Client(timeout=timeout_s, headers={"Authorization": f"Bearer {api_key}"} if api_key else None)

    def _payload(self, messages, temperature, max_tokens, json_schema, logprobs, mode) -> dict:
        p = {"model": self.model, "messages": messages, "temperature": temperature, "max_tokens": max_tokens}
        if logprobs:
            p.update(logprobs=True, top_logprobs=1)
        if json_schema is not None:
            if mode == "json_schema":
                p["json_schema"] = json_schema
            else:
                p["response_format"] = {"type": "json_schema",
                                        "json_schema": {"name": "output", "strict": True, "schema": json_schema}}
        return p

    def chat(self, messages, *, temperature=0.0, max_tokens=512, json_schema=None, logprobs=False) -> LLMResponse:
        t0 = time.perf_counter()
        modes = [self.mode] if self.mode != "auto" or json_schema is None else ["response_format", "json_schema"]
        last: Exception | None = None
        for mode in modes:
            r = self._http.post(f"{self.base_url}/chat/completions",
                                json=self._payload(messages, temperature, max_tokens, json_schema, logprobs, mode))
            if 400 <= r.status_code < 500 and mode != modes[-1]:
                last = httpx.HTTPStatusError(r.text[:200], request=r.request, response=r)
                continue
            r.raise_for_status()
            if json_schema is not None and self.mode == "auto":
                self.mode = mode
            choice = r.json()["choices"][0]
            lp = (choice.get("logprobs") or {}).get("content")
            return LLMResponse(choice["message"]["content"] or "", lp, round((time.perf_counter() - t0) * 1000, 1))
        raise last or RuntimeError("no structured-output mode accepted")

    def health(self) -> bool:
        root = self.base_url.removesuffix("/v1")
        for path in (f"{root}/health", f"{self.base_url}/models"):
            try:
                if self._http.get(path, timeout=5).status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
        return False


def field_probability(text: str, logprobs: list[dict] | None, field_name: str) -> float | None:
    """Probability the model assigned to the *value* of a top-level JSON string field, from token logprobs:
    exp(sum of logprobs of the tokens covering the value). None when logprobs are missing."""
    if not logprobs:
        return None
    m = re.search(rf'"{re.escape(field_name)}"\s*:\s*"([^"]*)"', text)
    if not m:
        return None
    start, end = m.span(1)
    pos, total, covered = 0, 0.0, False
    for tok in logprobs:
        t = tok.get("token", "")
        tstart, pos = pos, pos + len(t)
        if pos > start and tstart < end:
            total += float(tok.get("logprob", 0.0))
            covered = True
        if tstart >= end:
            break
    return math.exp(total) if covered else None


# ------------------------------------------------------------------------------------------- FakeLLM

Rule = tuple[str | Callable[[str], bool], str | Callable[[list[dict]], str]]


@dataclass
class FakeLLM:
    """Deterministic stand-in for a llama.cpp server. `rules` are (pattern, reply) pairs checked in order
    against the whole prompt text (system + user); the reply may be a string or a function of the messages.
    Implements both our LLMClient (`chat`) and Mercer's async LLMBackend (`generate`, `generate_batch`)."""
    rules: list[Rule] = field(default_factory=list)
    default: str = ""
    logprob: float | None = None  # if set, return one token per char with this logprob each
    calls: list[dict] = field(default_factory=list)

    def _reply(self, messages: list[dict]) -> str:
        prompt = "\n".join(str(m.get("content", "")) for m in messages)
        for pat, reply in self.rules:
            hit = pat(prompt) if callable(pat) else re.search(pat, prompt, re.S | re.I)
            if hit:
                return reply(messages) if callable(reply) else reply
        return self.default

    def chat(self, messages, *, temperature=0.0, max_tokens=512, json_schema=None, logprobs=False) -> LLMResponse:
        out = self._reply(messages)
        self.calls.append({"messages": messages, "json_schema": json_schema, "temperature": temperature, "reply": out})
        lp = [{"token": ch, "logprob": self.logprob} for ch in out] if logprobs and self.logprob is not None else None
        return LLMResponse(out, lp, 0.0)

    async def generate(self, prompt: str, system: str, temperature: float = 0.0, max_tokens: int = 1024,
                       is_json: bool = False) -> str:
        return self.chat([{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                         temperature=temperature, max_tokens=max_tokens).text

    async def generate_batch(self, prompts, system, temperature=0.0, max_tokens=1024, is_json=False) -> list[str]:
        temps = temperature if isinstance(temperature, list) else [temperature] * len(prompts)
        return list(await asyncio.gather(*(self.generate(p, system, t, max_tokens, is_json) for p, t in zip(prompts, temps))))


def json_reply(obj) -> str:
    return json.dumps(obj)
