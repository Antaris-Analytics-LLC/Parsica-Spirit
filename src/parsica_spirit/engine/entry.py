"""Memory entry - the atomic unit of memory."""

import hashlib
from datetime import datetime
from typing import Dict, List, Optional


class MemoryEntry:
    """Single memory unit with metadata, decay, sentiment, and confidence.

    Additional fields
    -----------------
    memory_type : str
        One of "episodic", "fact", "preference", "procedure", "mistake"
        or any custom string.  Defaults to "episodic".
    type_metadata : dict
        Type-specific structured data.  For mistakes this holds
        {"what_happened": ..., "correction": ..., "root_cause": ..., "severity": ...}.
        Empty for other types unless the caller populates it.
    """

    __slots__ = (
        "content", "source", "line", "category", "created",
        "last_accessed", "access_count", "importance", "confidence",
        "sentiment", "tags", "related", "hash",
        "memory_type", "type_metadata",
        # session isolation
        "session_id",
        # agent scoping
        "agent_id",
        # LLM enrichment
        "enriched_summary", "search_keywords",
        # doc2query - LLM-generated search queries this memory answers
        "search_queries",
        # knowledge ingestion provenance
        "source_url", "content_hash",
        # fact decomposition provenance
        "provenance", "parent_hash",
        # cross-channel recency recall
        "source_channel",
    )

    def __init__(
        self,
        content: str,
        source: str = "",
        line: int = 0,
        category: str = "general",
        created: str = None,
        memory_type: str = "episodic",
        session_id: str = "",
        agent_id: str = "",
        source_channel: str = "",
        **kwargs,  # absorb channel_id and future fields gracefully
    ):
        self.content = content
        self.source = source
        self.line = line
        self.category = category
        self.created = created or datetime.now().isoformat()
        self.last_accessed = self.created
        self.access_count: int = 0
        self.importance: float = 1.0
        self.confidence: float = 0.5
        self.sentiment: Dict[str, float] = {}
        self.tags: List[str] = []
        self.related: List[str] = []
        self.hash = hashlib.md5(
            f"{source}:{line}:{content}".encode()
        ).hexdigest()[:12]
        self.memory_type: str = memory_type
        self.type_metadata: Dict = {}
        # session isolation
        self.session_id: str = session_id
        # agent scoping
        self.agent_id: str = agent_id
        # LLM enrichment
        self.enriched_summary: str = ""
        self.search_keywords: List[str] = []
        self.search_queries: List[str] = []  # doc2query
        # knowledge ingestion provenance
        self.source_url: Optional[str] = None
        self.content_hash: Optional[str] = None
        # fact decomposition provenance
        self.provenance: str = "self"
        self.parent_hash: str = ""
        # cross-channel recency recall
        self.source_channel: str = source_channel

    def recompute_hash(self) -> str:
        """Recompute the id from the CURRENT (source, line, content). Call after mutating content
        (e.g. a post-scrub commit) so the stored id reflects what is actually on disk - never a
        restored or pre-scrub hash, which would poison dedup."""
        self.hash = hashlib.md5(
            f"{self.source}:{self.line}:{self.content}".encode()
        ).hexdigest()[:12]
        return self.hash

    # -- serialisation --------------------------------------------------------

    def to_dict(self) -> Dict:
        d = {
            "hash": self.hash,
            "content": self.content,
            "source": self.source,
            "line": self.line,
            "category": self.category,
            "created": self.created,
            "last_accessed": self.last_accessed,
            "access_count": self.access_count,
            "importance": round(self.importance, 4),
            "confidence": round(self.confidence, 4),
            "sentiment": self.sentiment,
            "tags": self.tags,
            "related": self.related,
        }
        # only write if non-default to keep file size stable
        if self.memory_type and self.memory_type != "episodic":
            d["memory_type"] = self.memory_type
        if self.type_metadata:
            d["type_metadata"] = self.type_metadata
        # session_id - only write if set
        if self.session_id:
            d["session_id"] = self.session_id
        # agent_id - only write if set
        if self.agent_id:
            d["agent_id"] = self.agent_id
        # LLM enrichment - only write if present
        if self.enriched_summary:
            d["enriched_summary"] = self.enriched_summary
        if self.search_keywords:
            d["search_keywords"] = self.search_keywords
        if self.search_queries:
            d["search_queries"] = self.search_queries
        # knowledge ingestion provenance
        if self.source_url is not None:
            d["source_url"] = self.source_url
        if self.content_hash is not None:
            d["content_hash"] = self.content_hash
        # fact decomposition provenance - only write if non-default
        if self.provenance and self.provenance != "self":
            d["provenance"] = self.provenance
        if self.parent_hash:
            d["parent_hash"] = self.parent_hash
        # cross-channel recency recall - only write if set
        if self.source_channel:
            d["source_channel"] = self.source_channel
        return d

    @classmethod
    def from_dict(cls, d: Dict) -> "MemoryEntry":
        """Trusted deserializer for the engine's OWN persisted records (store load,
        internally-generated synthesis output). It intentionally restores fields verbatim,
        including the stored hash - so it must NEVER be fed UNTRUSTED external data. External
        ingress (raw text, imported files) must enter through ingest()/import_from(), which
        scrub content, validate memory_type, and recompute the hash before anything persists.
        """
        m = cls(
            d["content"], d.get("source", ""), d.get("line", 0),
            d.get("category", "general"), d.get("created"),
            memory_type=d.get("memory_type", "episodic"),
        )
        m.last_accessed = d.get("last_accessed", m.created)
        m.access_count = d.get("access_count", 0)
        m.importance = d.get("importance", 1.0)
        m.confidence = d.get("confidence", 0.5)
        m.sentiment = d.get("sentiment", {})
        m.tags = d.get("tags", [])
        m.related = d.get("related", [])
        m.hash = d.get("hash", m.hash)
        m.type_metadata = d.get("type_metadata", {})
        # session_id
        m.session_id = d.get("session_id", "")
        # agent_id
        m.agent_id = d.get("agent_id", "")
        # LLM enrichment
        m.enriched_summary = d.get("enriched_summary", "")
        m.search_keywords = d.get("search_keywords", [])
        m.search_queries = d.get("search_queries", [])
        # knowledge ingestion provenance
        m.source_url = d.get("source_url")
        m.content_hash = d.get("content_hash")
        # fact decomposition provenance
        m.provenance = d.get("provenance", "self")
        m.parent_hash = d.get("parent_hash", "")
        # cross-channel recency recall
        m.source_channel = d.get("source_channel", "")
        return m

    def __repr__(self) -> str:
        return (
            f"<Memory {self.hash} type={self.memory_type} "
            f"score={self.importance:.2f} conf={self.confidence:.2f}>"
        )
