"""Confidence scoring & contradiction detection."""

from typing import List, Tuple
from .entry import MemoryEntry

NEGATION_PAIRS = [
    ("working", "broken"), ("stable", "crashed"), ("complete", "incomplete"),
    ("fixed", "broken"), ("success", "failed"), ("approved", "rejected"),
    ("cheap", "expensive"), ("fast", "slow"), ("available", "unavailable"),
    ("secure", "vulnerable"), ("online", "offline"), ("enabled", "disabled"),
]


class ConfidenceEngine:
    """Track memory reliability and detect contradictions."""

    @staticmethod
    def find_contradictions(
        memories: List[MemoryEntry],
    ) -> List[Tuple[MemoryEntry, MemoryEntry, str]]:
        """Detect potential contradictions via opposing keyword pairs on shared topics."""
        contradictions = []
        for i, m1 in enumerate(memories):
            m1_lower = m1.content.lower()
            for m2 in memories[i + 1:]:
                if m1.source == m2.source and abs(m1.line - m2.line) < 5:
                    continue
                m2_lower = m2.content.lower()
                for pos, neg in NEGATION_PAIRS:
                    if (pos in m1_lower and neg in m2_lower) or \
                       (neg in m1_lower and pos in m2_lower):
                        shared = set(m1.tags) & set(m2.tags)
                        if shared:
                            reason = (
                                f"Conflicting states ({pos}/{neg}) "
                                f"on topic: {', '.join(shared)}"
                            )
                            contradictions.append((m1, m2, reason))
                            break
        return contradictions
