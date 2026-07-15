"""Temporal reasoning - time-aware memory queries."""

import re
from datetime import datetime
from typing import List, Optional
from .entry import MemoryEntry

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


class TemporalEngine:
    """Time-aware memory queries and narrative construction."""

    @staticmethod
    def extract_dates(text: str) -> List[str]:
        """Pull all YYYY-MM-DD dates from text."""
        return DATE_RE.findall(text)

    @staticmethod
    def on_date(memories: List[MemoryEntry], date: str) -> List[MemoryEntry]:
        """Return memories associated with a specific date."""
        results = []
        for m in memories:
            if date in m.source or date in m.content or m.created.startswith(date):
                results.append(m)
        return results

    @staticmethod
    def between(memories: List[MemoryEntry], start: str, end: str) -> List[MemoryEntry]:
        """Return memories created between two dates (inclusive)."""
        return sorted(
            [m for m in memories if start <= m.created[:10] <= end],
            key=lambda x: x.created,
        )

    @staticmethod
    def narrative(memories: List[MemoryEntry], topic: Optional[str] = None) -> str:
        """Build a chronological narrative from memories, optionally filtered by topic."""
        relevant = memories
        if topic:
            t = topic.lower()
            relevant = [m for m in memories if t in m.content.lower()]

        relevant = sorted(relevant, key=lambda x: x.created)
        if not relevant:
            return f"No memories found{' about ' + topic if topic else ''}."

        lines: List[str] = []
        current_date = ""
        for m in relevant:
            d = m.created[:10]
            if d != current_date:
                current_date = d
                lines.append(f"\n### {d}")
            lines.append(f"- {m.content[:200]}")
        return "\n".join(lines)
