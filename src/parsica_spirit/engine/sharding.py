"""
Sharding System - Split memories across multiple files for better performance.

Shards memories by:
- Date: YYYY-MM format (e.g. 2026-02)  
- Topic: Based on dominant tags/categories

Benefits:
- Faster search (only load relevant shards)
- Better concurrent access (multiple processes can work on different shards)
- Easier backup/archival (archive old date shards)
"""

import json
import logging
import os
import re
from collections import defaultdict, OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from .entry import MemoryEntry

logger = logging.getLogger("parsica_spirit.engine")

_UNSAFE_KEY_RE = re.compile(r"[^a-z0-9_-]+")


def _safe_shard_key(s: str, fallback: str) -> str:
    """Reduce an arbitrary caller-supplied category/tag to a filesystem-safe token - no path
    separators, no '..', no drive letters - so a shard filename can never traverse out of
    shards/. Mirrors namespace._validate_name's allowlist; an empty result falls back."""
    s = _UNSAFE_KEY_RE.sub("-", str(s or "").lower()).strip("-")[:64]
    return s or fallback


def _chmod(path, mode):
    try:
        os.chmod(path, mode)
    except (OSError, NotImplementedError):
        pass


class ShardKey:
    """Represents a unique shard identifier."""
    
    def __init__(self, date_key: str, topic_key: str = "general"):
        # Sanitize both components: they come from caller-supplied category/tags and land
        # verbatim in the shard filename, so an unsanitized value ("x/../../etc") is a
        # path-traversal write. Legitimate alphanumeric topics are unchanged.
        self.date_key = _safe_shard_key(date_key, "unknown")    # YYYY-MM format
        self.topic_key = _safe_shard_key(topic_key, "general")  # topic category
    
    @property 
    def filename(self) -> str:
        """Generate filename for this shard."""
        return f"shard_{self.date_key}_{self.topic_key}.json"
    
    def __str__(self) -> str:
        return f"{self.date_key}:{self.topic_key}"
    
    def __hash__(self) -> int:
        return hash((self.date_key, self.topic_key))
    
    def __eq__(self, other) -> bool:
        return (self.date_key, self.topic_key) == (other.date_key, other.topic_key)


class ShardIndex:
    """Tracks which memories are in which shards."""
    
    def __init__(self, workspace: str):
        self.workspace = workspace
        self.index_path = os.path.join(workspace, "memory_index.json")
        self.shards: Dict[ShardKey, Dict] = {}  # shard_key -> metadata
        self._load_index()
    
    def _load_index(self):
        """Load shard index from disk."""
        if not os.path.exists(self.index_path):
            return

        # L-1 fix: guard against corrupt/truncated index files
        try:
            with open(self.index_path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, FileNotFoundError, OSError) as e:
            import sys
            print(f"[parsica-spirit engine] WARNING: shard index corrupt/missing, rebuilding: {e}", file=sys.stderr)
            return
        
        for shard_info in data.get("shards", []):
            key = ShardKey(shard_info["date_key"], shard_info["topic_key"])
            self.shards[key] = {
                "count": shard_info["count"],
                "first_entry": shard_info["first_entry"],
                "last_entry": shard_info["last_entry"], 
                "topics": set(shard_info["topics"]),
                "size_bytes": shard_info.get("size_bytes", 0)
            }
    
    def save_index(self):
        """Save shard index to disk."""
        data = {
            "version": "0.4.0",
            "updated_at": datetime.now().isoformat(),
            "total_shards": len(self.shards),
            "shards": [
                {
                    "date_key": key.date_key,
                    "topic_key": key.topic_key,
                    "filename": key.filename,
                    "count": metadata["count"],
                    "first_entry": metadata["first_entry"],
                    "last_entry": metadata["last_entry"],
                    "topics": list(metadata["topics"]),
                    "size_bytes": metadata["size_bytes"]
                }
                for key, metadata in self.shards.items()
            ]
        }
        
        from .utils import atomic_write_json
        atomic_write_json(self.index_path, data)
    
    def add_shard(self, key: ShardKey, memories: List[MemoryEntry]):
        """Register a new shard in the index."""
        if not memories:
            return
            
        topics = set()
        for memory in memories:
            topics.update(memory.tags)
            if memory.category:
                topics.add(memory.category)
        
        self.shards[key] = {
            "count": len(memories),
            "first_entry": memories[0].created,
            "last_entry": memories[-1].created,
            "topics": topics,
            "size_bytes": 0  # Will be calculated when shard is saved
        }
    
    def find_relevant_shards(self, 
                           query: str, 
                           date_range: Optional[Tuple[str, str]] = None,
                           topic_filter: Optional[str] = None) -> List[ShardKey]:
        """Find shards that might contain relevant memories."""
        relevant = []
        
        # Extract potential topics from query
        query_lower = query.lower()
        query_words = set(re.findall(r'\w{3,}', query_lower))
        
        for key, metadata in self.shards.items():
            # Date filtering
            if date_range:
                start_date, end_date = date_range
                if key.date_key < start_date[:7] or key.date_key > end_date[:7]:
                    continue
            
            # Topic filtering  
            if topic_filter and topic_filter.lower() not in key.topic_key.lower():
                continue
            
            # Query relevance - check if any query words match shard topics
            shard_topics = {t.lower() for t in metadata["topics"]}
            if query_words & shard_topics:
                relevant.append(key)
            # Also include general shards and date-matching shards
            elif key.topic_key == "general" or len(query_words) == 0:
                relevant.append(key)
        
        # Sort by date (newest first) and topic relevance
        relevant.sort(key=lambda k: (k.date_key, k.topic_key), reverse=True)
        return relevant
    
    def get_stats(self) -> Dict:
        """Get sharding statistics."""
        total_memories = sum(meta["count"] for meta in self.shards.values())
        total_size = sum(meta["size_bytes"] for meta in self.shards.values())
        
        date_distribution = defaultdict(int)
        topic_distribution = defaultdict(int)
        
        for key, meta in self.shards.items():
            date_distribution[key.date_key] += meta["count"]
            topic_distribution[key.topic_key] += meta["count"]
        
        return {
            "total_shards": len(self.shards),
            "total_memories": total_memories,
            "total_size_bytes": total_size,
            "avg_memories_per_shard": total_memories / len(self.shards) if self.shards else 0,
            "date_distribution": dict(date_distribution),
            "topic_distribution": dict(topic_distribution)
        }


class ShardManager:
    """Manages memory sharding and retrieval."""
    
    def __init__(self, workspace: str):
        self.workspace = workspace
        self.shards_dir = os.path.join(workspace, "shards")
        os.makedirs(self.shards_dir, exist_ok=True)
        _chmod(self.shards_dir, 0o700)   # store dir private to the owning user (no-op on Windows)

        self.index = ShardIndex(workspace)
        self._shard_cache: Dict[ShardKey, List[MemoryEntry]] = OrderedDict()
    
    def create_shard_key(self, memory: MemoryEntry) -> ShardKey:
        """Determine which shard a memory belongs to."""
        # Extract date (YYYY-MM format)
        date_key = memory.created[:7]  # "2026-02-15" -> "2026-02"
        
        # Determine topic based on category and tags
        topic_key = "general"
        
        if memory.category and memory.category != "general":
            topic_key = memory.category.lower()
        elif memory.tags:
            # Use the first meaningful tag as topic
            meaningful_tags = [t for t in memory.tags if len(t) > 2 and not t.startswith("@")]
            if meaningful_tags:
                topic_key = meaningful_tags[0].lower()
        
        return ShardKey(date_key, topic_key)
    
    def save_shard(self, key: ShardKey, memories: List[MemoryEntry]) -> str:
        """Save a shard to disk, preserving enrichment data from existing shards.
        
        If a shard file already exists on disk with enrichment fields
        (search_keywords, enriched_summary) that are missing from the
        in-memory entries, those fields are merged back before writing.
        This prevents external enrichment scripts' data from being
        silently overwritten by save cycles that loaded stale entries.
        """
        shard_path = os.path.join(self.shards_dir, key.filename)
        
        # -- Merge enrichment fields from existing shard --------------
        _ENRICHMENT_FIELDS = ("search_keywords", "enriched_summary", "search_queries")
        if os.path.exists(shard_path):
            try:
                with open(shard_path) as _f:
                    existing = json.load(_f)
                # Build hash->enrichment lookup from disk
                disk_enrichment = {}
                for em in existing.get("memories", []):
                    h = em.get("hash")
                    if h:
                        enrich = {}
                        for field in _ENRICHMENT_FIELDS:
                            val = em.get(field)
                            if val:
                                enrich[field] = val
                        if enrich:
                            disk_enrichment[h] = enrich
                # Merge into in-memory entries where missing
                if disk_enrichment:
                    for mem in memories:
                        enrich = disk_enrichment.get(mem.hash)
                        if enrich:
                            if not mem.search_keywords and "search_keywords" in enrich:
                                mem.search_keywords = enrich["search_keywords"]
                            if not mem.enriched_summary and "enriched_summary" in enrich:
                                mem.enriched_summary = enrich["enriched_summary"]
                            if not mem.search_queries and "search_queries" in enrich:
                                mem.search_queries = enrich["search_queries"]
            except (json.JSONDecodeError, OSError):
                pass  # Corrupted/missing file - skip merge, proceed with save

        # -- Cross-shard enrichment recovery (H-1) ----------------------------
        # If the shard file didn't exist yet (new month), the above merge block
        # ran no lookup. Scan other existing shard files to recover enrichment
        # for any entries still missing it. Only triggered when at least one
        # entry is unenriched, keeping the fast path free of overhead.
        missing_hashes = {
            mem.hash for mem in memories
            if not getattr(mem, "enriched_summary", None)
            and not getattr(mem, "search_queries", None)
        }
        if missing_hashes:
            try:
                for fname in os.listdir(self.shards_dir):
                    if not fname.startswith("shard_") or not fname.endswith(".json"):
                        continue
                    # Skip the current shard file
                    if fname == key.filename:
                        continue
                    other_path = os.path.join(self.shards_dir, fname)
                    try:
                        with open(other_path) as _of:
                            other_data = json.load(_of)
                        for em in other_data.get("memories", []):
                            h = em.get("hash")
                            if h not in missing_hashes:
                                continue
                            enrich = {}
                            for field in _ENRICHMENT_FIELDS:
                                val = em.get(field)
                                if val:
                                    enrich[field] = val
                            if enrich:
                                # Apply to matching in-memory entry
                                for mem in memories:
                                    if mem.hash == h:
                                        if not mem.search_keywords and "search_keywords" in enrich:
                                            mem.search_keywords = enrich["search_keywords"]
                                        if not mem.enriched_summary and "enriched_summary" in enrich:
                                            mem.enriched_summary = enrich["enriched_summary"]
                                        if not mem.search_queries and "search_queries" in enrich:
                                            mem.search_queries = enrich["search_queries"]
                                        missing_hashes.discard(h)
                                        break
                        if not missing_hashes:
                            break  # All recovered, stop scanning
                    except (json.JSONDecodeError, OSError):
                        continue  # Skip unreadable shards
            except OSError:
                pass  # shards_dir not listable - skip cross-shard scan

        data = {
            "shard_key": str(key),
            "date_key": key.date_key,
            "topic_key": key.topic_key, 
            "version": "0.4.0",
            "saved_at": datetime.now().isoformat(),
            "count": len(memories),
            "memories": [m.to_dict() for m in memories]
        }
        
        from .utils import atomic_write_json
        atomic_write_json(shard_path, data)
        _chmod(shard_path, 0o600)   # shard content private to the owning user
        
        # Update index with actual file size
        file_size = os.path.getsize(shard_path)
        if key in self.index.shards:
            self.index.shards[key]["size_bytes"] = file_size
        
        return shard_path
    
    def load_shard(self, key: ShardKey) -> List[MemoryEntry]:
        """Load memories from a shard."""
        # Check cache first
        if key in self._shard_cache:
            self._shard_cache.move_to_end(key)  # LRU: mark as most-recently-used
            return self._shard_cache[key]
        
        shard_path = os.path.join(self.shards_dir, key.filename)
        if not os.path.exists(shard_path):
            return []
        
        try:
            with open(shard_path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            # tolerant-on-read: one corrupt/truncated shard must NOT brick the whole store
            # (construction loads every shard). Skip it; the rest still load.
            logger.warning("skipping unreadable shard %s: %s", shard_path, exc)
            return []

        memories = [MemoryEntry.from_dict(m) for m in data.get("memories", [])]
        
        # Cache the loaded shard (but limit cache size)
        if len(self._shard_cache) > 10:  # Keep max 10 shards in memory
            # Remove oldest cache entry
            oldest_key = next(iter(self._shard_cache))
            del self._shard_cache[oldest_key]
        
        self._shard_cache[key] = memories
        return memories
    
    def shard_memories(self, memories: List[MemoryEntry]) -> Dict[ShardKey, List[MemoryEntry]]:
        """Split memories into shards."""
        shards = defaultdict(list)

        for memory in memories:
            key = self.create_shard_key(memory)
            shards[key].append(memory)

        return dict(shards)

    def delete_obsolete_shards(self, expected_keys: Set[ShardKey]) -> int:
        """Delete shard files/index entries that are no longer part of current truth."""
        removed = 0
        expected_filenames = {key.filename for key in expected_keys}

        for shard_key in list(self.index.shards.keys()):
            if shard_key not in expected_keys:
                self.index.shards.pop(shard_key, None)

        for path in Path(self.shards_dir).glob("shard_*.json"):
            if path.name not in expected_filenames:
                try:
                    path.unlink()
                    removed += 1
                except OSError as _e:
                    import sys
                    print(f"[parsica-spirit engine] WARNING: failed to remove obsolete shard {path}: {_e}", file=sys.stderr)

        self._shard_cache = OrderedDict(
            (key, value) for key, value in self._shard_cache.items() if key in expected_keys
        )
        return removed
    
    def search_shards(self, 
                     query: str,
                     limit: int = 20,
                     date_range: Optional[Tuple[str, str]] = None,
                     topic_filter: Optional[str] = None) -> List[MemoryEntry]:
        """Search across relevant shards."""
        # Find which shards to search
        relevant_shards = self.index.find_relevant_shards(query, date_range, topic_filter)
        
        all_results = []
        query_lower = query.lower()
        
        # Search each relevant shard
        for shard_key in relevant_shards[:5]:  # Limit to 5 shards max for performance
            memories = self.load_shard(shard_key)
            
            # Simple text matching within shard
            for memory in memories:
                content_lower = memory.content.lower()
                if query_lower in content_lower:
                    all_results.append(memory)
                    if len(all_results) >= limit:
                        break
            
            if len(all_results) >= limit:
                break
        
        return all_results[:limit]
    
    def get_all_memories(self, limit: Optional[int] = None) -> List[MemoryEntry]:
        """Load all memories from all shards (use sparingly)."""
        all_memories = []
        
        for shard_key in self.index.shards.keys():
            memories = self.load_shard(shard_key)
            all_memories.extend(memories)
            
            if limit and len(all_memories) >= limit:
                break
        
        return all_memories[:limit] if limit else all_memories
    
    def compact_shards(self, max_shard_size: int = 1000):
        """Merge small shards and split large ones."""
        # compact_shards() currently reports shard health but does not yet perform
        # merge or split operations - that lifecycle is not implemented.
        import warnings
        warnings.warn(
            "[parsica-spirit engine] compact_shards() does not yet perform merge/split operations. "
            "Use the returned stats to inspect shard health.",
            stacklevel=2,
        )

        # -- Shard health reporting --------------------------------------
        health = {
            "total_shards": len(self.index.shards),
            "small_shards": 0,
            "large_shards": 0,
            "healthy_shards": 0,
            "total_entries": 0,
            "smallest_shard_count": None,
            "largest_shard_count": None,
            "fragmentation_ratio": 0.0,
        }
        for key, meta in self.index.shards.items():
            count = meta.get("count", 0)
            health["total_entries"] += count
            if health["smallest_shard_count"] is None or count < health["smallest_shard_count"]:
                health["smallest_shard_count"] = count
            if health["largest_shard_count"] is None or count > health["largest_shard_count"]:
                health["largest_shard_count"] = count
            if count < 100:  # Intentional policy: shards under 100 entries are "small"
                health["small_shards"] += 1
            elif count > max_shard_size:
                health["large_shards"] += 1
            else:
                health["healthy_shards"] += 1
        if health["total_shards"] > 0:
            health["fragmentation_ratio"] = round(
                health["small_shards"] / health["total_shards"], 3
            )
        # Return the computed shard-health stats.
        return health