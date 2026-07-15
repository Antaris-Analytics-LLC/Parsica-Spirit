"""
Parsica Spirit engine - Memory Graph
=============================

Knowledge graph built incrementally from ingested memories.
Entities extracted at write-time, relationships inferred from
co-occurrence and proximity.

At query time, the graph boosts BM25 results whose entities are
graph-connected to entities found in the query.

Storage: {workspace}/.memory_graph.json (human-readable JSON)

Zero dependencies. Non-fatal everywhere.
"""

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from parsica_spirit.engine.entity_extractor import Entity, EntityExtractor, _canonical


# -- Data structures ----------------------------------------------------------

@dataclass
class GraphNode:
    """An entity node in the memory graph."""
    canonical: str          # normalised entity text (lookup key)
    display: str            # original text (most confident mention)
    entity_type: str        # PERSON / PROJECT / CONCEPT / DATE / VERSION / URL
    mention_count: int = 0  # number of memory entries that mention this entity
    entry_ids: List[str] = field(default_factory=list)   # memory hashes
    first_seen: float = 0.0
    last_seen: float = 0.0


@dataclass
class GraphEdge:
    """A directed relationship between two entity nodes."""
    source: str             # canonical entity
    relation: str           # co_mentioned / created_by / depends_on / etc.
    target: str             # canonical entity
    weight: float = 1.0     # higher = stronger relationship
    entry_ids: List[str] = field(default_factory=list)   # entries that support this edge


class MemoryGraph:
    """
    Incrementally-built knowledge graph from memory entries.

    Graph is persisted as JSON. Load is lazy (only on first access).
    All public methods are non-fatal.

    Usage:
        graph = MemoryGraph(workspace)
        graph.index_entry(entry_hash, entry_content)
        boosted = graph.boost_results(query, bm25_results)
        paths = graph.find_path("alice", "parsica-spirit")
    """

    GRAPH_FILE = ".memory_graph.json"
    SCHEMA_VERSION = 1

    def __init__(self, workspace: str):
        self._workspace = workspace
        self._path = os.path.join(workspace, self.GRAPH_FILE)
        self._extractor = EntityExtractor()
        self._nodes: Dict[str, GraphNode] = {}    # canonical -> GraphNode
        self._edges: Dict[str, GraphEdge] = {}    # "src::rel::tgt" -> GraphEdge
        self._loaded = False

    # -- Persistence ----------------------------------------------------------

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._load()
            self._loaded = True

    def _load(self) -> None:
        """Load graph from disk. Silently starts fresh on corruption."""
        try:
            if os.path.exists(self._path):
                with open(self._path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if data.get('version') == self.SCHEMA_VERSION:
                    for can, nd in data.get('nodes', {}).items():
                        self._nodes[can] = GraphNode(**nd)
                    for key, ed in data.get('edges', {}).items():
                        self._edges[key] = GraphEdge(**ed)
        except Exception:
            pass  # start fresh

    def save(self) -> None:
        """Persist graph to disk."""
        try:
            self._ensure_loaded()
            os.makedirs(self._workspace, exist_ok=True)
            data = {
                'version': self.SCHEMA_VERSION,
                'nodes': {can: asdict(nd) for can, nd in self._nodes.items()},
                'edges': {key: asdict(ed) for key, ed in self._edges.items()},
            }
            tmp = self._path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self._path)
        except Exception:
            pass

    # -- Indexing -------------------------------------------------------------

    def index_entry(self, entry_hash: str, content: str) -> int:
        """
        Extract entities and relationships from a memory entry and add
        them to the graph. Returns number of entities indexed.
        """
        try:
            self._ensure_loaded()
            if not content or not entry_hash:
                return 0

            entities = self._extractor.extract(content)
            entities = self._extractor.merge_aliases(entities)
            relations = self._extractor.extract_relations(content, entities)

            now = time.time()

            # Upsert nodes
            for ent in entities:
                can = ent.canonical
                if can in self._nodes:
                    node = self._nodes[can]
                    node.mention_count += 1
                    node.last_seen = now
                    if entry_hash not in node.entry_ids:
                        node.entry_ids.append(entry_hash)
                    # Update display to higher-confidence mention
                    if ent.confidence > 0.7:
                        node.display = ent.text
                else:
                    self._nodes[can] = GraphNode(
                        canonical=can,
                        display=ent.text,
                        entity_type=ent.entity_type,
                        mention_count=1,
                        entry_ids=[entry_hash],
                        first_seen=now,
                        last_seen=now,
                    )

            # Upsert edges
            for src, rel, tgt in relations:
                if src not in self._nodes or tgt not in self._nodes:
                    continue
                key = f"{src}::{rel}::{tgt}"
                if key in self._edges:
                    self._edges[key].weight += 0.5
                    if entry_hash not in self._edges[key].entry_ids:
                        self._edges[key].entry_ids.append(entry_hash)
                else:
                    self._edges[key] = GraphEdge(
                        source=src, relation=rel, target=tgt,
                        weight=1.0, entry_ids=[entry_hash],
                    )

            return len(entities)
        except Exception:
            return 0

    def remove_entry(self, entry_hash: str) -> None:
        """Remove an entry's contributions from the graph."""
        try:
            self._ensure_loaded()
            for node in list(self._nodes.values()):
                if entry_hash in node.entry_ids:
                    node.entry_ids.remove(entry_hash)
                    node.mention_count = max(0, node.mention_count - 1)
            # Remove nodes with no remaining entries
            self._nodes = {k: v for k, v in self._nodes.items()
                           if v.entry_ids or v.mention_count > 0}
            # Remove edges with no remaining entries
            for key, edge in list(self._edges.items()):
                if entry_hash in edge.entry_ids:
                    edge.entry_ids.remove(entry_hash)
                if not edge.entry_ids:
                    del self._edges[key]
        except Exception:
            pass

    # -- Query ----------------------------------------------------------------

    def entities_in_query(self, query: str) -> List[str]:
        """Extract entity canonicals found in a query string."""
        try:
            self._ensure_loaded()
            entities = self._extractor.extract(query)
            entities = self._extractor.merge_aliases(entities)
            # Return only those we've actually seen in the graph
            return [e.canonical for e in entities if e.canonical in self._nodes]
        except Exception:
            return []

    def neighbors(
        self, canonical: str, max_hops: int = 2, relation: str = None
    ) -> Dict[str, float]:
        """
        BFS from a node. Returns {canonical: relevance_score} for all
        reachable nodes within max_hops. Score decays by hop.
        Optionally filter by relation type.
        """
        try:
            self._ensure_loaded()
            if canonical not in self._nodes:
                return {}

            visited: Dict[str, float] = {canonical: 1.0}
            queue: List[Tuple[str, int, float]] = [(canonical, 0, 1.0)]

            while queue:
                current, hop, score = queue.pop(0)
                if hop >= max_hops:
                    continue
                for key, edge in self._edges.items():
                    if relation and edge.relation != relation:
                        continue
                    neighbor = None
                    if edge.source == current:
                        neighbor = edge.target
                    elif edge.target == current:
                        neighbor = edge.source
                    if neighbor and neighbor not in visited:
                        # Score: decay by hop, boost by edge weight
                        new_score = score * 0.6 * min(edge.weight, 2.0)
                        visited[neighbor] = new_score
                        queue.append((neighbor, hop + 1, new_score))

            del visited[canonical]  # don't include query entity itself
            return visited
        except Exception:
            return {}

    def boost_results(
        self,
        query: str,
        results: list,          # List[SearchResult] from search.py
        boost_factor: float = 0.3,
        max_hops: int = 2,
    ) -> list:
        """
        Apply graph-based score boost to BM25 search results.

        For each result, check if its memory entry mentions entities that
        are graph-connected to entities in the query. Connected entries
        get a score boost proportional to graph relevance.

        boost_factor: max additional score fraction (0.3 = up to 30% boost)
        """
        try:
            self._ensure_loaded()
            if not results:
                return results

            query_entities = self.entities_in_query(query)
            if not query_entities:
                return results  # no graph signal - return unchanged

            # Build graph relevance map: entity_canonical -> relevance
            graph_relevance: Dict[str, float] = {}
            for qe in query_entities:
                for neighbor, score in self.neighbors(qe, max_hops=max_hops).items():
                    existing = graph_relevance.get(neighbor, 0.0)
                    graph_relevance[neighbor] = max(existing, score)

            if not graph_relevance:
                return results

            # Apply boost to results
            boosted = []
            for result in results:
                try:
                    entry_hash = getattr(result.entry, 'hash', None)
                    content = getattr(result.entry, 'content', '')
                    entry_entities = self._extractor.extract(content)
                    entry_entities = self._extractor.merge_aliases(entry_entities)

                    # Graph boost = max relevance of any entity in this entry
                    max_relevance = 0.0
                    for ent in entry_entities:
                        rel = graph_relevance.get(ent.canonical, 0.0)
                        max_relevance = max(max_relevance, rel)

                    if max_relevance > 0:
                        # Boost score by up to boost_factor
                        result.score += result.score * boost_factor * max_relevance

                    boosted.append(result)
                except Exception:
                    boosted.append(result)

            # Re-sort by boosted score
            return sorted(boosted, key=lambda r: r.score, reverse=True)
        except Exception:
            return results

    def relationship_search(
        self, subject: str, relation: str = None, obj: str = None
    ) -> List[Tuple[str, str, str, List[str]]]:
        """
        Graph traversal search: find relationships involving the given entities.

        Examples:
          relationship_search("alice")              -> all relationships involving alice
          relationship_search("alice", "created_by") -> alice created_by what?
          relationship_search("alice", obj="parsica") -> how is alice related to parsica?

        Returns: List of (source, relation, target, supporting_entry_ids)
        """
        try:
            self._ensure_loaded()
            subject_can = _canonical(subject)
            obj_can = _canonical(obj) if obj else None
            results = []

            for key, edge in self._edges.items():
                match = False
                if edge.source == subject_can or edge.target == subject_can:
                    match = True
                if match and relation and edge.relation != relation:
                    match = False
                if match and obj_can:
                    if edge.source != obj_can and edge.target != obj_can:
                        match = False
                if match:
                    results.append((edge.source, edge.relation,
                                    edge.target, edge.entry_ids))

            return results
        except Exception:
            return []

    def find_path(
        self, source: str, target: str, max_hops: int = 3
    ) -> Optional[List[str]]:
        """
        BFS path between two entities.
        Returns list of canonicals from source -> target, or None if no path.
        """
        try:
            self._ensure_loaded()
            src_can = _canonical(source)
            tgt_can = _canonical(target)

            if src_can not in self._nodes or tgt_can not in self._nodes:
                return None
            if src_can == tgt_can:
                return [src_can]

            # BFS
            queue: List[List[str]] = [[src_can]]
            visited: Set[str] = {src_can}

            while queue:
                path = queue.pop(0)
                current = path[-1]

                if len(path) > max_hops + 1:
                    continue

                # Find neighbors
                for edge in self._edges.values():
                    neighbor = None
                    if edge.source == current:
                        neighbor = edge.target
                    elif edge.target == current:
                        neighbor = edge.source

                    if neighbor and neighbor not in visited:
                        new_path = path + [neighbor]
                        if neighbor == tgt_can:
                            return new_path
                        visited.add(neighbor)
                        queue.append(new_path)

            return None  # no path found within max_hops
        except Exception:
            return None

    # -- Stats ----------------------------------------------------------------

    def get_stats(self) -> dict:
        """Return graph statistics."""
        try:
            self._ensure_loaded()
            type_counts: Dict[str, int] = {}
            for node in self._nodes.values():
                type_counts[node.entity_type] = \
                    type_counts.get(node.entity_type, 0) + 1

            return {
                'node_count': len(self._nodes),
                'edge_count': len(self._edges),
                'entity_types': type_counts,
                'top_entities': sorted(
                    [{'canonical': n.canonical, 'mentions': n.mention_count}
                     for n in self._nodes.values()],
                    key=lambda x: x['mentions'],
                    reverse=True,
                )[:10],
            }
        except Exception:
            return {'node_count': 0, 'edge_count': 0}

    def get_node(self, canonical: str) -> Optional[GraphNode]:
        """Get a specific entity node."""
        try:
            self._ensure_loaded()
            return self._nodes.get(_canonical(canonical))
        except Exception:
            return None
