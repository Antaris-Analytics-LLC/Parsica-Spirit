"""
Cross-Memory Clustering for the Parsica Spirit engine.

Builds lightweight entity co-reference clusters so that related memories
can boost each other at query time.  A memory about "pricing discussion
with Alex" and a memory about "Alex approved the Q3 plan" share the
entity "Alex" and will be clustered together.

Design constraints (protecting the indexing fast path):
  - Clusters are pre-computed at index time -> O(n) per memory.
  - Query-time lookup is a hash table get -> O(1).
  - Cluster size is hard-capped (default 8) to prevent runaway expansion.
  - Only memories sharing at least one named entity are clustered.
  - Zero external dependencies.

Usage
-----
    clusterer = MemoryClusterer(max_cluster_size=8)
    clusterer.build_clusters(memories)

    # At query time, after scoring:
    boosted = clusterer.boost_neighbors(
        scored_results, top_k=10, boost_factor=0.15
    )
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple


# ---------------------------------------------------------------------------
# Entity extraction - lightweight, no ML
# ---------------------------------------------------------------------------

# Patterns that are almost certainly entities in memory content
_ENTITY_PATTERNS = [
    # Capitalized multi-word names: "Alex Rivera", "Google Cloud"
    re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b'),
    # Single capitalized word that's not sentence-start (heuristic: preceded by
    # lowercase or punctuation+space)
    re.compile(r'(?<=[a-z.,;:!?]\s)([A-Z][a-z]{2,})\b'),
    # ALL-CAPS acronyms 2-6 chars: "AWS", "API", "GCP"
    re.compile(r'\b([A-Z]{2,6})\b'),
    # CamelCase identifiers: "DataPipeline", "parsica-spirit"
    re.compile(r'\b([A-Z][a-z]+[A-Z][A-Za-z]*)\b'),
]

# Common false positives to skip
_ENTITY_STOPWORDS = frozenset({
    'The', 'This', 'That', 'These', 'Those', 'Here', 'There',
    'What', 'When', 'Where', 'Which', 'Who', 'How', 'Why',
    'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday',
    'Saturday', 'Sunday', 'January', 'February', 'March',
    'April', 'May', 'June', 'July', 'August', 'September',
    'October', 'November', 'December',
    'None', 'True', 'False', 'Yes', 'No',
    'AM', 'PM', 'EST', 'PST', 'UTC', 'GMT',
    'OK', 'US', 'UK', 'EU',
    'NOTE', 'TODO', 'FIXME', 'HACK', 'XXX',
    'IF', 'OR', 'AND', 'NOT', 'BUT', 'FOR', 'THE',
})


def extract_entities(text: str) -> Set[str]:
    """Extract likely named entities from text.

    Returns a set of normalized (lowercased) entity strings.
    Cheap and imperfect - but fast and zero-dependency.
    """
    entities: Set[str] = set()
    for pattern in _ENTITY_PATTERNS:
        for match in pattern.finditer(text):
            raw = match.group(1).strip()
            if raw in _ENTITY_STOPWORDS:
                continue
            if len(raw) < 2:
                continue
            if raw.replace(' ', '').isdigit():
                continue
            entities.add(raw.lower())
    return entities


# ---------------------------------------------------------------------------
# MemoryClusterer
# ---------------------------------------------------------------------------

class MemoryClusterer:
    """Build and query entity-based memory clusters.

    Parameters
    ----------
    max_cluster_size : int
        Hard cap on memories per entity cluster.  When a shared entity links
        more memories than this, only the most recent are kept.
    """

    def __init__(self, max_cluster_size: int = 8) -> None:
        if max_cluster_size < 2:
            raise ValueError("max_cluster_size must be >= 2")
        self.max_cluster_size = max_cluster_size

        # entity (lowercased) -> set of memory hashes
        self._entity_to_hashes: Dict[str, Set[str]] = defaultdict(set)
        # memory hash -> set of entity strings
        self._hash_to_entities: Dict[str, Set[str]] = {}
        # memory hash -> set of neighbor hashes (pre-computed)
        self._neighbors: Dict[str, Set[str]] = defaultdict(set)
        # Track build state
        self._built = False

    # ------------------------------------------------------------------
    # Index-time: build clusters
    # ------------------------------------------------------------------

    def build_clusters(self, memories: list) -> None:
        """Pre-compute entity clusters from a list of MemoryEntry objects.

        Call this at index time (alongside ``SearchEngine.build_index``).
        Runs in O(n x e) where n = number of memories and e = avg entities
        per memory (typically 2-5).
        """
        self._entity_to_hashes.clear()
        self._hash_to_entities.clear()
        self._neighbors.clear()

        # Pass 1: extract entities per memory
        for mem in memories:
            entities = extract_entities(mem.content)
            # Also extract from tags if present
            if hasattr(mem, 'tags') and mem.tags:
                for tag in mem.tags:
                    tag_entities = extract_entities(tag)
                    entities.update(tag_entities)
                    # Tags themselves are often entity-like
                    if len(tag) >= 2 and tag.lower() not in {'general', 'episodic'}:
                        entities.add(tag.lower())

            self._hash_to_entities[mem.hash] = entities
            for ent in entities:
                self._entity_to_hashes[ent].add(mem.hash)

        # Pass 2: enforce cluster size cap (keep most recent)
        hash_to_created: Dict[str, str] = {}
        for mem in memories:
            hash_to_created[mem.hash] = getattr(mem, 'created', '') or ''

        for ent, hashes in self._entity_to_hashes.items():
            if len(hashes) > self.max_cluster_size:
                sorted_hashes = sorted(
                    hashes,
                    key=lambda h: hash_to_created.get(h, ''),
                    reverse=True,
                )
                self._entity_to_hashes[ent] = set(
                    sorted_hashes[:self.max_cluster_size]
                )

        # Pass 3: pre-compute neighbor sets
        for mem_hash, entities in self._hash_to_entities.items():
            neighbors: Set[str] = set()
            for ent in entities:
                cluster = self._entity_to_hashes.get(ent, set())
                neighbors.update(cluster)
            neighbors.discard(mem_hash)  # don't include self
            self._neighbors[mem_hash] = neighbors

        self._built = True

    # ------------------------------------------------------------------
    # Query-time: boost neighbors
    # ------------------------------------------------------------------

    def boost_neighbors(
        self,
        results: list,
        top_k: int = 10,
        boost_factor: float = 0.15,
    ) -> list:
        """Apply cluster-neighbor boost to search results.

        For each result in the top-K, find its cluster neighbors in the
        full result set.  Neighbors below top-K get a score boost
        proportional to ``boost_factor x anchor_score``.

        Parameters
        ----------
        results : list
            List of SearchResult objects, sorted by score descending.
        top_k : int
            Only the top-K results act as "anchors" that propagate boosts.
        boost_factor : float
            Fraction of anchor score to add to neighbors (0.0-1.0).

        Returns
        -------
        list
            Re-sorted results with boosted scores.  Original objects are
            NOT mutated; new SearchResult objects are created where needed.
        """
        if not self._built or not results:
            return results

        # Build hash->result index
        hash_to_idx: Dict[str, int] = {}
        for i, r in enumerate(results):
            h = getattr(r.entry, 'hash', None)
            if h:
                hash_to_idx[h] = i

        # Collect boosts (additive, may accumulate from multiple anchors)
        boosts: Dict[int, float] = defaultdict(float)

        for r in results[:top_k]:
            anchor_hash = getattr(r.entry, 'hash', None)
            if not anchor_hash:
                continue
            neighbors = self._neighbors.get(anchor_hash, set())
            for nb_hash in neighbors:
                if nb_hash in hash_to_idx:
                    idx = hash_to_idx[nb_hash]
                    if idx >= top_k:  # only boost results outside top-K
                        boosts[idx] += r.score * boost_factor

        if not boosts:
            return results

        # Apply boosts - avoid circular import by reconstructing inline
        new_results = list(results)
        for idx, boost_amount in boosts.items():
            old = new_results[idx]
            new_score = old.score + boost_amount
            # Duck-type a new result object to avoid import
            new_results[idx] = _BoostedResult(old, new_score, boost_amount)

        new_results.sort(key=lambda r: r.score, reverse=True)
        return new_results

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def stats(self) -> Dict:
        """Return clustering statistics."""
        if not self._built:
            return {"built": False}
        cluster_sizes = [len(v) for v in self._entity_to_hashes.values()]
        neighbor_counts = [len(v) for v in self._neighbors.values()]
        return {
            "built": True,
            "unique_entities": len(self._entity_to_hashes),
            "memories_with_entities": len(self._hash_to_entities),
            "avg_cluster_size": round(
                sum(cluster_sizes) / max(len(cluster_sizes), 1), 2
            ),
            "max_cluster_size": max(cluster_sizes) if cluster_sizes else 0,
            "avg_neighbors_per_memory": round(
                sum(neighbor_counts) / max(len(neighbor_counts), 1), 2
            ),
        }

    def get_cluster_for(self, memory_hash: str) -> Set[str]:
        """Return the set of neighbor hashes for a given memory."""
        return self._neighbors.get(memory_hash, set())

    def get_entities_for(self, memory_hash: str) -> Set[str]:
        """Return entities extracted from a given memory."""
        return self._hash_to_entities.get(memory_hash, set())


# ---------------------------------------------------------------------------
# _BoostedResult - duck-typed wrapper to avoid circular SearchResult import
# ---------------------------------------------------------------------------

class _BoostedResult:
    """Thin wrapper around a SearchResult that overrides its score.

    Proxies all attribute access to the original result except ``score``,
    ``matched_terms``, and ``explanation``.
    """

    __slots__ = ('_orig', 'score', 'matched_terms', 'explanation')

    def __init__(self, orig, new_score: float, boost_amount: float):
        self._orig = orig
        self.score = new_score
        self.matched_terms = list(orig.matched_terms) + ['cluster_boost']
        self.explanation = (
            orig.explanation + f' | cluster_boost=+{boost_amount:.3f}'
        )

    def __getattr__(self, name):
        # Guard: if _orig isn't set yet (during __init__ / copy), raise AttributeError
        # instead of recursing.  __slots__ means _orig may not exist yet.
        if name == '_orig':
            raise AttributeError(name)
        return getattr(self._orig, name)

    def __copy__(self):
        """Shallow copy that avoids copy.copy recursion (Python 3.14 compatibility)."""
        new = object.__new__(_BoostedResult)
        object.__setattr__(new, '_orig', self._orig)
        object.__setattr__(new, 'score', self.score)
        object.__setattr__(new, 'matched_terms', self.matched_terms)
        object.__setattr__(new, 'explanation', self.explanation)
        return new

    def __repr__(self):
        return (
            f"<BoostedResult score={self.score:.3f} "
            f"'{self.entry.content[:50]}...'>"
        )


# ---------------------------------------------------------------------------
# ClusterIndex - SearchEngine integration adapter
# ---------------------------------------------------------------------------

class ClusterIndex(MemoryClusterer):
    """Thin adapter so SearchEngine can call .build() and .apply_cluster_boost().

    SearchEngine uses:
        self._cluster_index.build(memories)          -> index time
        self._cluster_index.apply_cluster_boost(...)  -> query time
        self._cluster_index._built                    -> guard check

    MemoryClusterer provides:
        .build_clusters(memories)
        .boost_neighbors(results, top_k, boost_factor)
        ._built
    """

    def build(self, memories: list) -> None:
        """Alias for build_clusters()."""
        self.build_clusters(memories)

    def apply_cluster_boost(
        self,
        results: list,
        top_k: int = 10,
        boost_factor: float = 0.15,
    ) -> list:
        """Alias for boost_neighbors(). Expects SearchResult objects."""
        return self.boost_neighbors(results, top_k=top_k, boost_factor=boost_factor)
