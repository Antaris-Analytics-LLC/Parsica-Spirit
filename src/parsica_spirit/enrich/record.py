"""parsica_spirit.enrich.record - the single canonical doc2query enrichment record.

This is the ONE definition of an enrichment, shared by the facade and the engine.
Neither may redefine its shape - that is the whole point of the leaf.

Contract:
  * FROZEN  - an EnrichmentRecord is immutable once built.
  * VERSIONED - `schema_version` is pinned and travels with the record.
  * CLOSED-ON-WRITE - `to_dict()` emits exactly the canonical fields, nothing more.
  * TOLERANT-ON-READ - `from_dict()` coerces odd shapes, drops unknown keys, and
    NEVER raises. Real stores accumulate messy records over versions; reading must
    never be the thing that breaks continuity.

The enrichment is the *envelope* (doc2query: summary/tags/keywords/search_queries).
Provenance (source, session, timestamps) rides on the memory *body*, not here -
that body/envelope separation is what lets the two travel and be re-derived
independently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence, Tuple

SCHEMA_VERSION = 1


def _as_str_tuple(value: Any) -> Tuple[str, ...]:
    """Coerce anything into a tuple of non-empty strings. Never raises."""
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, Sequence):
        out = []
        for item in value:
            if item is None:
                continue
            s = item if isinstance(item, str) else str(item)
            if s:
                out.append(s)
        return tuple(out)
    return ()


@dataclass(frozen=True)
class EnrichmentRecord:
    """A doc2query enrichment: the four fields a retriever weights, plus version."""

    summary: str = ""
    tags: Tuple[str, ...] = ()
    keywords: Tuple[str, ...] = ()
    search_queries: Tuple[str, ...] = ()
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict:
        """Closed-on-write: exactly the canonical fields, lists for JSON."""
        return {
            "schema_version": self.schema_version,
            "summary": self.summary,
            "tags": list(self.tags),
            "keywords": list(self.keywords),
            "search_queries": list(self.search_queries),
        }

    @classmethod
    def from_dict(cls, data: Optional[Mapping[str, Any]]) -> "EnrichmentRecord":
        """Tolerant-on-read: coerce, drop unknowns, never raise."""
        d: Mapping[str, Any] = data or {}
        summary = d.get("summary")
        try:
            version = int(d.get("schema_version") or SCHEMA_VERSION)
        except (TypeError, ValueError):
            version = SCHEMA_VERSION
        return cls(
            summary=summary if isinstance(summary, str) else ("" if summary is None else str(summary)),
            tags=_as_str_tuple(d.get("tags")),
            keywords=_as_str_tuple(d.get("keywords")),
            search_queries=_as_str_tuple(d.get("search_queries")),
            schema_version=version,
        )

    def is_empty(self) -> bool:
        """True when the record carries no retrievable signal."""
        return not (self.summary or self.tags or self.keywords or self.search_queries)
