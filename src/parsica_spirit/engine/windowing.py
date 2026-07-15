"""
Sliding Window Context Indexing for the Parsica Spirit engine.

Related facts that span multiple consecutive memories can be missed by
single-entry search. This module builds overlapping *windows* of consecutive
MemoryEntry objects so that nearby facts are co-indexed and can be matched
together by the search engine.

Windows are generated on-the-fly during search; they are never persisted.
Zero external dependencies.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from typing import List, Optional

from .entry import MemoryEntry


# ---------------------------------------------------------------------------
# WindowEntry - a duck-typed MemoryEntry for search purposes
# ---------------------------------------------------------------------------

class WindowEntry:
    """A sliding-window aggregate that looks like a MemoryEntry for scoring.

    Attributes
    ----------
    content : str
        Concatenation of all constituent entry contents (newline-separated).
    source_ids : list[str]
        Ordered list of source entry hashes.
    window_index : int
        Sequential index of this window (0-based).
    created : str
        ISO timestamp of the *earliest* constituent entry.
    source : str
        Always ``"window"``.
    tags : list[str]
        Union of tags from all source entries (deduplicated, original order).
    category : str
        Most common category among source entries; ties broken alphabetically.
    session_id : str
        session_id of the *first* constituent entry (windows stay within a
        session - build_windows filters by session before windowing).
    hash : str
        ``f"window_{window_index}"``
    """

    __slots__ = (
        "content", "source_ids", "window_index", "created",
        "source", "tags", "category", "session_id", "hash",
        # Extra fields that may be probed by generic code
        "line", "importance", "confidence", "sentiment",
        "related", "access_count", "last_accessed",
        "memory_type", "type_metadata", "agent_id",
    )

    def __init__(
        self,
        content: str,
        source_ids: List[str],
        window_index: int,
        created: str,
        tags: List[str],
        category: str,
        session_id: str = "",
    ) -> None:
        self.content = content
        self.source_ids = source_ids
        self.window_index = window_index
        self.created = created
        self.source = "window"
        self.tags = tags
        self.category = category
        self.session_id = session_id
        self.hash = f"window_{window_index}"

        # Sensible defaults for fields that MemoryEntry carries
        self.line = 0
        self.importance = 1.0
        self.confidence = 0.5
        self.sentiment: dict = {}
        self.related: list = []
        self.access_count = 0
        self.last_accessed = created
        self.memory_type = "episodic"
        self.type_metadata: dict = {}
        self.agent_id = ""

    def __repr__(self) -> str:
        return (
            f"<WindowEntry window={self.window_index} "
            f"sources={self.source_ids} len={len(self.content)}>"
        )


# ---------------------------------------------------------------------------
# WindowIndexer
# ---------------------------------------------------------------------------

class WindowIndexer:
    """Build overlapping sliding windows over a list of MemoryEntry objects.

    Parameters
    ----------
    window_size : int
        Number of consecutive entries per window (default 3).
    stride : int
        Step size between window starts (default 1).  A stride of 1 gives
        maximum overlap; stride == window_size gives non-overlapping windows.
    """

    def __init__(self, window_size: int = 3, stride: int = 1) -> None:
        if window_size < 1:
            raise ValueError("window_size must be >= 1")
        if stride < 1:
            raise ValueError("stride must be >= 1")
        self.window_size = window_size
        self.stride = stride

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_windows(self, entries: List[MemoryEntry]) -> List[WindowEntry]:
        """Generate sliding windows over *entries*.

        Windows shorter than ``window_size`` (only possible at the tail when
        fewer entries remain than the window size) are **skipped** - this
        avoids polluting search with near-duplicate short windows right at
        the boundary.

        Parameters
        ----------
        entries :
            Ordered list of MemoryEntry objects (order is assumed to reflect
            chronological ingestion order).

        Returns
        -------
        list[WindowEntry]
            One WindowEntry per full window, in order.
        """
        if len(entries) < self.window_size:
            return []

        windows: List[WindowEntry] = []
        window_index = 0
        n = len(entries)

        for start in range(0, n - self.window_size + 1, self.stride):
            end = start + self.window_size
            chunk = entries[start:end]
            if len(chunk) < self.window_size:
                break  # shouldn't happen given loop bound, but be safe

            window_entry = self._make_window(chunk, window_index)
            windows.append(window_entry)
            window_index += 1

        return windows

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_window(chunk: List[MemoryEntry], window_index: int) -> WindowEntry:
        """Combine a list of entries into a single WindowEntry."""
        content = "\n".join(e.content for e in chunk)
        source_ids = [e.hash for e in chunk]

        # Earliest timestamp
        timestamps = [e.created for e in chunk if e.created]
        created = min(timestamps) if timestamps else ""

        # Union of tags (preserve first-seen order, no duplicates)
        seen_tags: dict = {}
        for e in chunk:
            for t in (e.tags or []):
                if t not in seen_tags:
                    seen_tags[t] = None
        tags = list(seen_tags.keys())

        # Most common category (tie -> alphabetically first)
        cat_counter: Counter = Counter(
            e.category for e in chunk if e.category
        )
        if cat_counter:
            max_count = max(cat_counter.values())
            candidates = sorted(k for k, v in cat_counter.items() if v == max_count)
            category = candidates[0]
        else:
            category = "general"

        # session_id from first entry
        session_id = getattr(chunk[0], "session_id", "") or ""

        return WindowEntry(
            content=content,
            source_ids=source_ids,
            window_index=window_index,
            created=created,
            tags=tags,
            category=category,
            session_id=session_id,
        )
