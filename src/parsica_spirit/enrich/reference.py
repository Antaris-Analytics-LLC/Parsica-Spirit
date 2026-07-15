"""A deterministic, zero-dependency reference EnricherPort.

This is NOT the production enricher (production enrichers typically call an external
model). It exists so the leaf is self-testable and demonstrable
WITHOUT any model or network, and so the EnricherPort contract always has a
known-good reference implementation. Pure term-frequency doc2query-lite:
same input -> same output, every time.
"""

from __future__ import annotations

import re
from collections import Counter

from .record import EnrichmentRecord

_WORD = re.compile(r"[A-Za-z0-9_]+")
_STOP = frozenset(
    "the a an and or but if then of to in on for with at by from is are was were "
    "be been being this that these those it its as not no do does did has have had "
    "i you he she we they me my your our their".split()
)
_SENT = re.compile(r"(?<=[.!?])\s+")


class ReferenceEnricher:
    """Deterministic term-frequency doc2query-lite. Implements EnricherPort."""

    def __init__(self, max_keywords: int = 8, max_tags: int = 5) -> None:
        self.max_keywords = max_keywords
        self.max_tags = max_tags

    def enrich(self, text: str) -> EnrichmentRecord:
        text = (text or "").strip()
        if not text:
            return EnrichmentRecord()
        tokens = [t.lower() for t in _WORD.findall(text)]
        content = [t for t in tokens if t not in _STOP and len(t) > 2]
        freq = Counter(content)
        # deterministic order: most frequent first, ties broken alphabetically
        ranked = [w for w, _ in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))]
        summary = _SENT.split(text, maxsplit=1)[0][:200]
        return EnrichmentRecord(
            summary=summary,
            tags=tuple(ranked[: self.max_tags]),
            keywords=tuple(ranked[: self.max_keywords]),
            search_queries=tuple(f"what about {w}?" for w in ranked[:3]),
        )
