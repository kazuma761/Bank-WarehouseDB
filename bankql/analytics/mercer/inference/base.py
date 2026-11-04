"""LLMBackend — protocol and default batch implementation."""

from __future__ import annotations

import asyncio
from typing import Protocol


class LLMBackend(Protocol):
    """Protocol for all LLM inference backends.

    Backends must implement `generate()`. `generate_batch()` has a default
    implementation that fans out to `generate()` via asyncio.gather — backends
    can override it if the underlying API supports true batch endpoints.

    `temperature` may be a single float (applied to every prompt) or a
    list[float] of the same length as `prompts` (per-prompt temperatures).
    Per-prompt temperatures are essential for genuine candidate diversity
    in multi-strategy generation (CHASE-SQL pattern).
    """

    async def generate(
        self,
        prompt: str,
        system: str,
        temperature: float = 0.0,
        max_tokens: int = 2048,
        is_json: bool = False,
    ) -> str:
        """Send a single prompt to the LLM and return the generated text."""
        ...

    async def generate_batch(
        self,
        prompts: list[str],
        system: str,
        temperature: float | list[float] = 0.0,
        max_tokens: int = 2048,
        is_json: bool = False,
    ) -> list[str]:
        """Default: concurrent fan-out over generate(). Override for native batch APIs.

        If `temperature` is a list, its length must match `prompts`.
        """
        temps = _expand_temperatures(temperature, len(prompts))
        results = await asyncio.gather(*(
            self.generate(p, system, t, max_tokens, is_json)
            for p, t in zip(prompts, temps, strict=True)
        ))
        return list(results)


def _expand_temperatures(
    temperature: float | list[float],
    n: int,
) -> list[float]:
    """Normalize a temperature spec into a per-prompt list of length n."""
    if isinstance(temperature, list):
        if len(temperature) != n:
            raise ValueError(
                f"temperature list length {len(temperature)} does not match "
                f"prompts length {n}"
            )
        return [float(t) for t in temperature]
    return [float(temperature)] * n
