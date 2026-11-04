"""Honest-evaluation guard (hard rule 5): no evaluation question or gold SQL may appear in any prompt,
few-shot example, verified-query library, glossary or semantic YAML."""
from __future__ import annotations

import re
from pathlib import Path


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower())).strip()


def prompt_corpus(root: str | Path) -> str:
    """Every text a model can see: our router prompts, Mercer's prompt files (incl. few-shots), semantic YAML."""
    root = Path(root)
    from bankql import router

    texts = [router.ROUTER_SYSTEM, router.REWRITE_SYSTEM, router.SUMMARY_SYSTEM]
    texts += [p.read_text(encoding="utf-8") for p in (root / "bankql/analytics/mercer/prompts").glob("*.py")]
    texts += [p.read_text(encoding="utf-8") for p in (root / "config/semantic").glob("*.yaml")]
    return _norm("\n".join(texts))


def leaks(items: list[dict], corpus: str, *, min_words: int = 6) -> list[dict]:
    """Items whose question or gold SQL (normalised) occurs in the corpus. Very short strings are ignored."""
    out = []
    for it in items:
        for field in ("question", "gold_sql"):
            s = _norm(str(it.get(field) or ""))
            if len(s.split()) >= min_words and s in corpus:
                out.append({"id": it.get("id"), "field": field})
    return out
