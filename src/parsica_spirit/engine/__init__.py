"""
Parsica - open-source persistent memory for AI agents (Parsica Spirit lean engine).

Store, search, decay, and consolidate agent memories. Standard library plus the
dependency-free contracts leaf; deterministic for a fixed store, configuration,
and reference time; transparent JSON storage.

This is the lean OSS engine surface. Several advanced capabilities are maintained in a
separate build and are not part of this lean engine. The model-free accumulating substrate
(graph, co-occurrence, PPMI, indexes, instrumentation) is kept - it is the heart.
"""

__version__ = "1.0.0"

from typing import List, TypedDict


class EnrichmentResult(TypedDict, total=False):
    """Return type for the doc2query enricher callable passed to MemorySystem."""

    tags: List[str]
    summary: str
    keywords: List[str]


# Core
from parsica_spirit.engine.core import MemorySystem
from parsica_spirit.engine.entry import MemoryEntry
from parsica_spirit.engine.decay import DecayEngine
from parsica_spirit.engine.sentiment import SentimentTagger
from parsica_spirit.engine.temporal import TemporalEngine
from parsica_spirit.engine.confidence import ConfidenceEngine
from parsica_spirit.engine.compression import CompressionEngine
from parsica_spirit.engine.forgetting import ForgettingEngine
from parsica_spirit.engine.consolidation import ConsolidationEngine
from parsica_spirit.engine.gating import InputGate
from parsica_spirit.engine.synthesis import KnowledgeSynthesizer

# Storage + indexing
from parsica_spirit.engine.sharding import ShardManager, ShardKey
from parsica_spirit.engine.migration import MigrationManager, Migration
from parsica_spirit.engine.indexing import IndexManager, SearchIndex, TagIndex, DateIndex

# Concurrency
from parsica_spirit.engine.locking import FileLock, LockTimeout
from parsica_spirit.engine.versioning import VersionTracker, ConflictError

# Search
from parsica_spirit.engine.search import SearchEngine, SearchResult, IntentReranker, QualifierAnalyzer
from parsica_spirit.engine.clustering import MemoryClusterer, ClusterIndex

# Context packets
from parsica_spirit.engine.context_packet import ContextPacket, ContextPacketBuilder

# Recovery
from parsica_spirit.engine.recovery import RecoveryConfig, RecoveryManager

# Memory types + namespace isolation
from parsica_spirit.engine.memory_types import MEMORY_TYPE_CONFIGS, get_type_config
from parsica_spirit.engine.namespace import NamespacedMemory, NamespaceManager

# Instrumentation + co-occurrence (the accumulating substrate)
from parsica_spirit.engine.instrumentation import (
    SearchContext,
    extract_memory_references,
)
from parsica_spirit.engine.cooccurrence import CooccurrenceIndex

# Semantic stack (model-free: category tagging, windowing)
from parsica_spirit.engine.category_tagger import CategoryTagger
from parsica_spirit.engine.windowing import WindowIndexer

# Tiered storage + manifest ingestion
from parsica_spirit.engine.tiers import TierManager
from parsica_spirit.engine.ingest_manifest import IngestManifest

# Graph intelligence + consolidation (the accumulating substrate)
from parsica_spirit.engine.entity_extractor import EntityExtractor, Entity
from parsica_spirit.engine.graph import MemoryGraph, GraphNode, GraphEdge
from parsica_spirit.engine.consolidation_runner import ConsolidationRunner, ConsolidationReport

# MemoryManager (enrichment is an injected callable; no provider clients ship)
from parsica_spirit.engine.manager import MemoryManager

__all__ = [
    "MemorySystem", "MemoryEntry", "EnrichmentResult",
    "DecayEngine", "SentimentTagger", "TemporalEngine", "ConfidenceEngine",
    "CompressionEngine", "ForgettingEngine", "ConsolidationEngine", "InputGate",
    "KnowledgeSynthesizer",
    "ShardManager", "ShardKey", "MigrationManager", "Migration",
    "IndexManager", "SearchIndex", "TagIndex", "DateIndex",
    "FileLock", "LockTimeout", "VersionTracker", "ConflictError",
    "SearchEngine", "SearchResult", "IntentReranker", "QualifierAnalyzer",
    "MemoryClusterer", "ClusterIndex",
    "ContextPacket", "ContextPacketBuilder",
    "RecoveryConfig", "RecoveryManager",
    "MEMORY_TYPE_CONFIGS", "get_type_config",
    "NamespacedMemory", "NamespaceManager",
    "SearchContext", "extract_memory_references",
    "CooccurrenceIndex",
    "CategoryTagger", "WindowIndexer",
    "TierManager", "IngestManifest",
    "EntityExtractor", "Entity", "MemoryGraph", "GraphNode", "GraphEdge",
    "ConsolidationRunner", "ConsolidationReport",
    "MemoryManager",
]
