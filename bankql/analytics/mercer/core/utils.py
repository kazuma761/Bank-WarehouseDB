"""Shared utilities for the Mercer pipeline."""

from __future__ import annotations

import re

# Matches ```sql ... ``` or ``` ... ``` blocks
_FENCE_RE = re.compile(r"^```(?:sql)?\s*\n?(.*?)\n?```\s*$", re.DOTALL | re.IGNORECASE)


def strip_fences(text: str) -> str:
    """Strip markdown code fences (```sql ... ``` or ``` ... ```) from LLM output."""
    stripped = text.strip()
    m = _FENCE_RE.match(stripped)
    return m.group(1).strip() if m else stripped


def strip_json_fences(text: str) -> str:
    """Remove markdown code fences that LLMs sometimes wrap around JSON responses."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        inner = lines[1:-1] if lines[-1].strip() == "```" else lines[1:]
        text = "\n".join(inner)
    return text.strip()
