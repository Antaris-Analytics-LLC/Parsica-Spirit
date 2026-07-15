"""Background memory processing - duplicates, clusters, insights."""

import re
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Tuple

from .entry import MemoryEntry
from .decay import DecayEngine
from .confidence import ConfidenceEngine


class ConsolidationEngine:
    """Background memory processing: duplicates, clusters, insights."""

    def __init__(self, decay: DecayEngine = None):
        self.decay = decay or DecayEngine()

    def find_duplicates(
        self, memories: List[MemoryEntry], threshold: float = 0.7,
        max_comparisons: int = 50000,
    ) -> List[Tuple[MemoryEntry, MemoryEntry]]:
        """Find EXACT-duplicate memories (identical normalized content).

        Bloat prevention removes only TRUE duplicates - memories that merely share
        wording but differ in content are all kept. (``threshold`` and
        ``max_comparisons`` are retained for API compatibility and are not used:
        matching is exact normalized-content, not overlap-based.)
        """
        dupes: List[Tuple[MemoryEntry, MemoryEntry]] = []
        seen: Dict[str, MemoryEntry] = {}
        for m in memories:
            norm = " ".join(m.content.split()).casefold()
            if norm in seen:
                dupes.append((seen[norm], m))
            else:
                seen[norm] = m
        return dupes

    def topic_clusters(self, memories: List[MemoryEntry]) -> Dict[str, List[str]]:
        """Group memories by shared tags."""
        clusters: Dict[str, List[str]] = defaultdict(list)
        for m in memories:
            for tag in m.tags:
                clusters[tag.lower()].append(m.hash)
        return {k: v for k, v in clusters.items() if len(v) >= 2}

    def run(self, memories: List[MemoryEntry]) -> Dict:
        """Full consolidation pass - returns a report."""
        now = datetime.now()
        scored = [(m, self.decay.score(m, now)) for m in memories]
        archive = [(m, s) for m, s in scored if self.decay.should_archive(m, now)]
        active = [(m, s) for m, s in scored if not self.decay.should_archive(m, now)]

        dupes = self.find_duplicates(memories)
        clusters = self.topic_clusters(memories)
        contradictions = ConfidenceEngine.find_contradictions(memories)

        return {
            "timestamp": now.isoformat(),
            "total": len(memories),
            "active": len(active),
            "archive_candidates": len(archive),
            "duplicates": len(dupes),
            "clusters": len(clusters),
            "contradictions": len(contradictions),
            "top_clusters": dict(
                sorted(clusters.items(), key=lambda x: len(x[1]), reverse=True)[:10]
            ),
            "archive_suggestions": [
                {"hash": m.hash, "preview": m.content[:100], "score": s}
                for m, s in archive[:20]
            ],
            "contradiction_details": [
                {"m1": m1.content[:100], "m2": m2.content[:100], "reason": r}
                for m1, m2, r in contradictions[:10]
            ],
        }
