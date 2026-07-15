"""Selective forgetting - privacy-aware memory deletion."""

from datetime import datetime
from typing import Dict, List, Tuple
from .entry import MemoryEntry


class ForgettingEngine:
    """Privacy-aware memory deletion with cascading removal and audit."""

    @staticmethod
    def forget_topic(
        memories: List[MemoryEntry], topic: str,
    ) -> Tuple[List[MemoryEntry], List[MemoryEntry]]:
        """Remove all memories related to a topic. Returns (kept, forgotten)."""
        t = topic.lower()
        kept, forgotten = [], []
        for m in memories:
            if t in m.content.lower() or t in " ".join(m.tags).lower():
                forgotten.append(m)
            else:
                kept.append(m)
        # Cascade: strip references to forgotten hashes
        gone = {m.hash for m in forgotten}
        for m in kept:
            m.related = [r for r in m.related if r not in gone]
        return kept, forgotten

    @staticmethod
    def forget_entity(
        memories: List[MemoryEntry], entity: str,
    ) -> Tuple[List[MemoryEntry], List[MemoryEntry]]:
        """Remove all memories mentioning a specific entity."""
        return ForgettingEngine.forget_topic(memories, entity)

    @staticmethod
    def forget_before(
        memories: List[MemoryEntry], date: str,
    ) -> Tuple[List[MemoryEntry], List[MemoryEntry]]:
        """Remove all memories created before a date (YYYY-MM-DD)."""
        kept, forgotten = [], []
        for m in memories:
            (forgotten if m.created[:10] < date else kept).append(m)
        return kept, forgotten

    @staticmethod
    def audit_log(forgotten: List[MemoryEntry]) -> Dict:
        """Create an audit record without preserving any content."""
        return {
            "operation": "selective_forget",
            "timestamp": datetime.now().isoformat(),
            "count": len(forgotten),
            "sources": list({m.source for m in forgotten}),
            "categories": list({m.category for m in forgotten}),
        }
