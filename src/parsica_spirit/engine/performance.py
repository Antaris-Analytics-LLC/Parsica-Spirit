"""
Performance subsystem for the Parsica Spirit engine.

Provides:
  - ReadCache     : LRU in-memory cache for search results (cache-hit = zero file I/O)
  - WALManager    : Write-ahead log for append-only ingestion with atomic compaction
  - PerformanceMonitor : Lightweight timing/counter stats
  - AccessTracker : Records which entries are accessed and boosts their search priority

Zero external dependencies - pure stdlib.
"""

import json
import os
import tempfile
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

# -----------------------------------------------------------------------------
# LRU Read Cache
# -----------------------------------------------------------------------------

class ReadCache:
    """LRU in-memory cache for search results.

    Stores arbitrary values keyed by query signature strings.
    Evicts least-recently-used entries when ``max_entries`` is exceeded.
    The cache is invalidated entirely on any write (ingest/flush) so stale
    results are never served.

    Args:
        max_entries: Maximum number of result-sets to hold in memory.
    """

    def __init__(self, max_entries: int = 1000):
        self.max_entries = max_entries
        self._cache: OrderedDict[str, Any] = OrderedDict()
        self._hits = 0
        self._misses = 0

    # -- public interface ------------------------------------------------

    def get(self, key: str) -> Optional[Any]:
        """Return cached value or ``None`` on miss."""
        if key not in self._cache:
            self._misses += 1
            return None
        self._cache.move_to_end(key)   # mark as most-recently-used
        self._hits += 1
        return self._cache[key]

    def put(self, key: str, value: Any) -> None:
        """Store a value, evicting the LRU entry if the cache is full."""
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = value
        if len(self._cache) > self.max_entries:
            self._cache.popitem(last=False)   # evict LRU

    def invalidate(self) -> None:
        """Clear the entire cache (called on any write)."""
        self._cache.clear()

    # -- stats -----------------------------------------------------------

    @property
    def hit_rate(self) -> float:
        total = self._hits + self._misses
        return self._hits / total if total > 0 else 0.0

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses

    def __len__(self) -> int:
        return len(self._cache)

    def __contains__(self, key: str) -> bool:
        return key in self._cache


# -----------------------------------------------------------------------------
# Write-Ahead Log (WAL)
# -----------------------------------------------------------------------------

class WALManager:
    """Append-only write-ahead log for safe, fast ingestion.

    Write path:
        ingest() -> WALManager.append()     (fast, no shard I/O)
        flush()  -> WALManager.compact_to() (batch write to shards)

    Safety guarantee: if the process crashes between ``append()`` and
    ``compact_to()``, the WAL file is replayed on the next ``load()``.
    Partial lines are skipped during replay (``json.JSONDecodeError``).

    Flush triggers (checked after every append):
        - every ``flush_interval`` appends (default 50)
        - when WAL file exceeds ``max_size_bytes`` (default 1 MB)

    Args:
        workspace: Root workspace directory.  WAL lives at
            ``{workspace}/.wal/pending.jsonl``.
        flush_interval: Number of appends before auto-flush is signalled.
        max_size_bytes: WAL file size that also triggers auto-flush.
    """

    WAL_DIR = ".wal"
    WAL_FILENAME = "pending.jsonl"

    def __init__(
        self,
        workspace: str,
        flush_interval: int = 50,
        max_size_bytes: int = 1_000_000,
    ):
        self.workspace = workspace
        self.wal_dir = os.path.join(workspace, self.WAL_DIR)
        self.wal_path = os.path.join(self.wal_dir, self.WAL_FILENAME)
        self.flush_interval = flush_interval
        self.max_size_bytes = max_size_bytes
        self._write_count = 0   # writes since last clear / currently pending

        os.makedirs(self.wal_dir, exist_ok=True)
        self._write_count = self._count_pending_lines_static(self.wal_path)

    # -- write path ------------------------------------------------------

    def append(self, entry_dict: Dict) -> None:
        """Atomically append one entry dict as a durable JSON line."""
        line = json.dumps(entry_dict, ensure_ascii=False) + "\n"
        newly = not os.path.exists(self.wal_path)
        with open(self.wal_path, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
        if newly:
            try:
                os.chmod(self.wal_path, 0o600)   # WAL content private to the owning user, like the shards (best-effort)
            except OSError:
                pass
        self._write_count += 1

    # -- read path (replay) -----------------------------------------------

    @staticmethod
    def _count_pending_lines_static(wal_path: str) -> int:
        """Return the number of valid WAL entries currently on disk."""
        if not os.path.exists(wal_path):
            return 0

        count = 0
        with open(wal_path, encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    json.loads(stripped)
                    count += 1
                except json.JSONDecodeError:
                    pass   # corrupted line - skip safely
        return count

    def load_pending(self) -> List[Dict]:
        """Return all valid pending entries from the WAL.

        Partial / corrupted JSON lines are skipped so a crash mid-write
        never prevents startup.
        """
        if not os.path.exists(self.wal_path):
            return []

        entries: List[Dict] = []
        with open(self.wal_path, encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    entries.append(json.loads(stripped))
                except json.JSONDecodeError:
                    pass   # corrupted line - skip safely
        return entries

    # -- maintenance ------------------------------------------------------

    def clear(self) -> None:
        """Remove the WAL file after successful compaction."""
        if os.path.exists(self.wal_path):
            os.remove(self.wal_path)
            try:
                dir_fd = os.open(self.wal_dir, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except (OSError, AttributeError):
                pass
        self._write_count = 0

    def replace_with_entries(self, entries: List[Dict]) -> None:
        """Atomically rewrite WAL to match the given durable pending set.

        Used by snapshot-first flush/compaction: persist the full current state
        to permanent storage, then rewrite the WAL to only the entries not yet
        included in that durable snapshot. This closes the crash window where a
        save could succeed but a stale WAL would later replay deleted/already-
        persisted entries.
        """
        fd, tmp_path = tempfile.mkstemp(dir=self.wal_dir, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                for entry in entries:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self.wal_path)
            try:
                dir_fd = os.open(self.wal_dir, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except (OSError, AttributeError):
                pass
            self._write_count = len(entries)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # -- introspection ----------------------------------------------------

    def size_bytes(self) -> int:
        if not os.path.exists(self.wal_path):
            return 0
        return os.path.getsize(self.wal_path)

    def pending_count(self) -> int:
        """Return the number of pending WAL entries.

        This remains O(1) during normal operation via the in-memory
        ``_write_count`` counter, but that counter is also rehydrated from the
        WAL file at startup so the value stays accurate after a restart.
        """
        return self._write_count

    def should_flush(self) -> bool:
        """Return True when auto-flush should be triggered."""
        return (
            self._write_count >= self.flush_interval
            or self.size_bytes() >= self.max_size_bytes
        )

    def exists(self) -> bool:
        return os.path.exists(self.wal_path)


# -----------------------------------------------------------------------------
# Performance Monitor
# -----------------------------------------------------------------------------

class PerformanceMonitor:
    """Lightweight stats collector for search timing and compaction events.

    Intentionally simple - all state is in-process.  No file I/O.
    """

    def __init__(self):
        self._search_count: int = 0
        self._total_search_ms: float = 0.0
        self._last_compaction: Optional[str] = None

    def record_search(self, elapsed_ms: float) -> None:
        self._search_count += 1
        self._total_search_ms += elapsed_ms

    def record_compaction(self) -> None:
        self._last_compaction = datetime.now(timezone.utc).isoformat()

    @property
    def search_count(self) -> int:
        return self._search_count

    @property
    def avg_search_time_ms(self) -> float:
        if self._search_count == 0:
            return 0.0
        return round(self._total_search_ms / self._search_count, 3)

    @property
    def last_compaction(self) -> Optional[str]:
        return self._last_compaction


# -----------------------------------------------------------------------------
# Access Tracker
# -----------------------------------------------------------------------------

class AccessTracker:
    """Records which memory entries are accessed and how often.

    Counts are persisted to ``{workspace}/access_counts.json`` on each
    ``save()`` call (typically after ``flush()``).  The file is a flat
    JSON object mapping ``entry_hash -> access_count``.

    Hot entries (high access counts) receive a recency boost in search
    so frequently consulted memories stay near the top of results.

    Args:
        workspace: Root workspace directory.
    """

    COUNTS_FILENAME = "access_counts.json"
    # Access-count boost parameters
    BOOST_MIN = 1.0   # minimum multiplier (untouched entry)
    BOOST_MAX = 1.5   # maximum multiplier (very hot entry)
    HOT_THRESHOLD = 10   # accesses before max boost kicks in

    def __init__(self, workspace: str):
        self.counts_path = os.path.join(workspace, self.COUNTS_FILENAME)
        self._counts: Dict[str, int] = self._load()

    # -- persistence ------------------------------------------------------

    def _load(self) -> Dict[str, int]:
        if not os.path.exists(self.counts_path):
            return {}
        try:
            with open(self.counts_path, encoding="utf-8") as fh:
                return json.load(fh)
        except (json.JSONDecodeError, IOError):
            return {}

    def save(self) -> None:
        from .utils import atomic_write_json
        atomic_write_json(self.counts_path, self._counts)

    # -- recording --------------------------------------------------------

    def record_access(self, entry_id: str) -> None:
        """Increment access count for an entry."""
        self._counts[entry_id] = self._counts.get(entry_id, 0) + 1

    def get_count(self, entry_id: str) -> int:
        return self._counts.get(entry_id, 0)

    # -- retrieval --------------------------------------------------------

    def get_top(self, n: int = 10) -> List[Tuple[str, int]]:
        """Return top-N ``(entry_id, count)`` pairs, sorted descending."""
        return sorted(self._counts.items(), key=lambda kv: kv[1], reverse=True)[:n]

    def boost_score(self, entry_id: str) -> float:
        """Return a score multiplier [1.0, 1.5] based on access count."""
        count = self.get_count(entry_id)
        if count <= 0:
            return self.BOOST_MIN
        ratio = min(count / self.HOT_THRESHOLD, 1.0)
        return self.BOOST_MIN + ratio * (self.BOOST_MAX - self.BOOST_MIN)

    def __len__(self) -> int:
        return len(self._counts)
