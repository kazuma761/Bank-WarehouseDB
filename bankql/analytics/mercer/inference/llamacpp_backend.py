"""llama.cpp async backend — calls the OpenAI-compatible endpoint exposed by
llama-cpp-python (``python -m llama_cpp.server``) or the native ``llama-server``
binary.

Both server modes expose the same ``/v1/chat/completions`` endpoint that
matches the OpenAI Chat API.

* Default port is 8080 (llama-server default).
* Health check probes ``/health`` first, then falls back to ``/v1/models``,
  because the two server modes expose different health endpoints.
* The ``model`` field in the request payload is sent as ``"local"``; both
  llama-server and llama-cpp-python ignore the model name and always use
  whatever was loaded at startup.

Retries on transient HTTP/connection failures with exponential backoff
(3 attempts, 1s–8s window).
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from bankql.analytics.mercer.core.logging import get_logger
from bankql.analytics.mercer.inference.base import LLMBackend

_logger = get_logger(__name__)


class LlamaCppBackend(LLMBackend):
    """Async inference backend for a locally-running llama.cpp server.

    Supports both server modes:
    * ``llama-server`` — the native C++ binary shipped with llama.cpp
    * ``python -m llama_cpp.server`` — llama-cpp-python's Python wrapper

    Both expose the same OpenAI-compatible ``/v1/chat/completions`` endpoint.
    """

    def __init__(self, base_url: str = "http://localhost:8080") -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=120.0)

    # ------------------------------------------------------------------
    # LLMBackend interface
    # ------------------------------------------------------------------

    async def generate(
        self,
        prompt: str,
        system: str,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        is_json: bool = False,
    ) -> str:
        """Generate a completion via the llama.cpp OpenAI-compatible endpoint."""
        return await self._call(prompt, system, temperature, max_tokens, is_json)

    async def generate_batch(
        self,
        prompts: list[str],
        system: str,
        temperature: float | list[float] = 0.0,
        max_tokens: int = 1024,
        is_json: bool = False,
    ) -> list[str]:
        """Fan out concurrently — llama.cpp queues requests internally.

        Honors per-prompt temperatures when ``temperature`` is a list.
        """
        from bankql.analytics.mercer.inference.base import _expand_temperatures
        temps = _expand_temperatures(temperature, len(prompts))
        results = await asyncio.gather(*(
            self.generate(p, system, t, max_tokens, is_json)
            for p, t in zip(prompts, temps, strict=True)
        ))
        return list(results)

    # ------------------------------------------------------------------
    # Health probe
    # ------------------------------------------------------------------

    async def health_check(self) -> bool:
        """Return True if the llama.cpp server is reachable and healthy.

        Tries ``/health`` (llama-server) then ``/v1/models``
        (llama-cpp-python) so both server modes work.
        """
        for path in ("/health", "/v1/models"):
            try:
                resp = await self._client.get(f"{self._base_url}{path}", timeout=5.0)
                if resp.status_code == 200:
                    return True
            except Exception:
                pass
        return False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _call(
        self,
        prompt: str,
        system: str,
        temperature: float,
        max_tokens: int,
        is_json: bool,
    ) -> str:
        payload: dict[str, Any] = {
            "model": "local",  # ignored by both server modes; required by spec
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if is_json:
            payload["response_format"] = {"type": "json_object"}

        resp = await self._client.post(
            f"{self._base_url}/v1/chat/completions",
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        return str(data["choices"][0]["message"]["content"])
