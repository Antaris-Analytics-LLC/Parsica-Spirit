"""The swarm batch envelope: the data contract for batch doc2query enrichment.

The production enricher is a fan-out of Claude Code subagents.
The leaf does NOT orchestrate that - dispatch, keys, retries, and concurrency live
in the caller. What the leaf owns is the CONTRACT: how a batch of texts is described
to the workers (`build_batch`), and how their raw output is parsed back into
validated EnrichmentRecords (`parse_batch`).

Validation happens here, at the leaf boundary, so that one malformed or hallucinated
worker output can never reach - and corrupt - a store. A missing or garbled item
degrades to an empty record; it never raises.
"""

from __future__ import annotations

from typing import Any, List, Mapping, Sequence

from .record import EnrichmentRecord

# The per-item field schema a worker is asked to return (doc2query).
ENRICH_ITEM_KEYS = ("i", "summary", "tags", "keywords", "search_queries")


def build_batch(texts: Sequence[str]) -> List[dict]:
    """Index a batch of texts for dispatch: ``[{"i": 0, "text": ...}, ...]``."""
    return [{"i": i, "text": (t or "")} for i, t in enumerate(texts)]


def parse_batch(raw: Any, batch_size: int) -> List[EnrichmentRecord]:
    """Parse raw worker output into a dense list of records (len == batch_size).

    Accepts ``{"enrichments": [...]}`` or a bare list. Items are placed by their
    ``i`` index; anything missing, out-of-range, or malformed becomes an empty
    record. Never raises.
    """
    items = raw.get("enrichments") if isinstance(raw, Mapping) else raw
    if not isinstance(items, (list, tuple)):
        items = []
    out: List[EnrichmentRecord] = [EnrichmentRecord() for _ in range(batch_size)]
    for item in items:
        if not isinstance(item, Mapping):
            continue
        try:
            idx = int(item.get("i"))
        except (TypeError, ValueError):
            continue
        if 0 <= idx < batch_size:
            out[idx] = EnrichmentRecord.from_dict(item)
    return out
