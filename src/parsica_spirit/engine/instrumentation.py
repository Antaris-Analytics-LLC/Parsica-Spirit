"""
Instrumentation layer for the Parsica Spirit engine.

Tracks which retrieved memories are actually used in agent responses,
enabling local, opt-in usage-based relevance signals (statistical only).

Zero dependencies. Zero overhead when disabled (callback=None).

Usage:
    from parsica_spirit.engine.instrumentation import SearchContext, extract_memory_references

    context = SearchContext(query="car maintenance", context_id="session_123")
    results = mem.search("car maintenance", instrumentation_context=context)

    # After agent generates response...
    used_ids = extract_memory_references(agent_response, context.retrieved_memory_ids)
    mem.mark_used(used_ids, context)
"""

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .audit import AuditLogger


# --- SearchContext ------------------------------------------------------------

@dataclass
class SearchContext:
    """Tracks a single search request through the retrieval -> response cycle.

    Created at search time, updated after the agent responds. Passed to
    mark_used() to signal which memories actually appeared in the output.

    Attributes:
        request_id: Unique identifier for this search request (uuid4).
        query: The original search query string.
        retrieved_memory_ids: IDs of memories returned by search().
        used_memory_ids: IDs of memories that appeared in the agent response.
        timestamp: Unix timestamp when the search was initiated.
        context_id: Optional session/conversation identifier.
        session_id: Optional session key for multi-session tracking.
        audit_logger: Optional AuditLogger for structured audit logging.
    """
    query: str
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    retrieved_memory_ids: List[str] = field(default_factory=list)
    used_memory_ids: List[str] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    context_id: Optional[str] = None
    session_id: Optional[str] = None
    audit_logger: Optional['AuditLogger'] = None

    def to_dict(self) -> Dict:
        """Serialize to dict for logging."""
        return {
            "request_id": self.request_id,
            "query": self.query,
            "retrieved_memory_ids": self.retrieved_memory_ids,
            "used_memory_ids": self.used_memory_ids,
            "timestamp": self.timestamp,
            "context_id": self.context_id,
            "session_id": self.session_id,
            # audit_logger is not serialized (not JSON-serializable)
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "SearchContext":
        """Deserialize from dict."""
        ctx = cls(query=data["query"])
        ctx.request_id = data.get("request_id", str(uuid.uuid4()))
        ctx.retrieved_memory_ids = data.get("retrieved_memory_ids", [])
        ctx.used_memory_ids = data.get("used_memory_ids", [])
        ctx.timestamp = data.get("timestamp", time.time())
        ctx.context_id = data.get("context_id")
        ctx.session_id = data.get("session_id")
        return ctx


# --- Memory Reference Extraction ---------------------------------------------

def extract_memory_references(
    text: str,
    retrieved_entries: list,
    min_overlap: int = 6,
) -> List[str]:
    """Find which retrieved memories were actually referenced in the agent response.

    Uses substring matching: if a significant phrase from a memory appears in
    the output text, that memory is considered "used." No LLM calls required.

    Args:
        text: The agent's response text to check.
        retrieved_entries: List of SearchResult or MemoryEntry objects from search().
        min_overlap: Minimum number of characters of overlap to count as a match.

    Returns:
        List of memory IDs (entry.hash) that appear to be used in the text.
    """
    if not text or not retrieved_entries:
        return []

    text_lower = text.lower()
    used_ids = []
    seen = set()

    for item in retrieved_entries:
        # Support both SearchResult and MemoryEntry
        entry = getattr(item, "entry", item)
        memory_id = getattr(entry, "hash", None)
        content = getattr(entry, "content", "")

        if not memory_id or not content:
            continue

        # Strategy 1: Direct substring match (longest common phrase >= min_overlap)
        if _has_significant_overlap(content.lower(), text_lower, min_overlap):
            if memory_id not in seen:
                used_ids.append(memory_id)
                seen.add(memory_id)
            continue

        # Strategy 2: Key phrase extraction (first N words of memory)
        key_phrase = _extract_key_phrase(content)
        if key_phrase and len(key_phrase) >= min_overlap and key_phrase in text_lower:
            if memory_id not in seen:
                used_ids.append(memory_id)
                seen.add(memory_id)

    return used_ids


def _has_significant_overlap(source: str, target: str, min_len: int) -> bool:
    """Check if any phrase of length >= min_len from source appears in target."""
    # Sliding window over source words
    words = source.split()
    if len(words) < 2:
        # Single-word content: only count if it meets min_len to avoid noisy matches
        s = source.strip()
        return len(s) >= min_len and s in target

    # Try progressively shorter phrases (start from full, trim ends)
    for start in range(len(words)):
        for end in range(len(words), start, -1):
            phrase = " ".join(words[start:end])
            if len(phrase) >= min_len and phrase in target:
                return True
            if len(phrase) < min_len:
                break  # Remaining phrases too short

    return False


# Stopwords for key phrase extraction (module-level to avoid per-call allocation)
_KEYPHRASE_STOPWORDS = frozenset({
    'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'and',
    'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with',
    'it', 'its', 'this', 'that', 'by', 'as', 'from', 'have',
    'has', 'had', 'not', 'no', 'can', 'will', 'do', 'did',
})


def _extract_key_phrase(content: str, max_words: int = 6) -> str:
    """Extract a key phrase from memory content for matching."""
    # Remove punctuation, take first N meaningful words
    words = re.findall(r'\b\w{3,}\b', content.lower())
    filtered = [w for w in words if w not in _KEYPHRASE_STOPWORDS]
    return " ".join(filtered[:max_words])
