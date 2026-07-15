"""
Tiered Memory Storage for the Parsica Spirit engine.

Three-tier architecture:
  Hot  (0-3 days):  Loaded into RAM on startup, always searched.
  Warm (3-14 days): NOT loaded on startup; loaded on-demand when hot
                    results are sparse (< WARM_FALLBACK_THRESHOLD).
  Cold (14+ days):  Never loaded automatically; only on explicit
                    search(include_cold=True).

Tier classification is based on shard file age via ISO calendar week
parsing - O(1) per shard, not O(n) per entry.

Shard hash tracking enables incremental sync: only shards whose content
has changed since the last startup need to be reloaded.

Usage::

    from parsica_spirit.engine.tiers import TierManager

    mgr = TierManager(workspace)
    hot, warm, cold = mgr.classify_shards(shard_names)
    warm_entries = mgr.load_warm(warm_shard_names)
    changed = mgr.get_changed_shards(all_shards)
"""

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

from .entry import MemoryEntry


# -- Tier boundary defaults (days from now) ---------------------------------
HOT_DAYS: int = 3
WARM_DAYS: int = 14
# Cold = anything older than WARM_DAYS

# Minimum hot-tier results before triggering warm-tier fallback search
WARM_FALLBACK_THRESHOLD: int = 3


class TierManager:
    """Classifies weekly shards into hot/warm/cold tiers and manages
    on-demand loading of the warm tier.

    Parameters
    ----------
    workspace:
        Root directory. Tier hash metadata stored as ``.tier_hashes.json``.
    hot_days:
        Entries newer than this are "hot" (default 3).
    warm_days:
        Entries older than ``hot_days`` but newer than this are "warm"
        (default 14). Entries older than ``warm_days`` are "cold".
    """

    SHARD_PREFIX: str = "memories_"
    SHARD_SUFFIX: str = ".jsonl"
    HASH_FILE: str = ".tier_hashes.json"

    def __init__(
        self,
        workspace: str,
        hot_days: int = HOT_DAYS,
        warm_days: int = WARM_DAYS,
    ) -> None:
        self.workspace = workspace
        self.hot_days = hot_days
        self.warm_days = warm_days
        self._hash_path = os.path.join(workspace, self.HASH_FILE)
        self._known_hashes: Dict[str, str] = self._load_hashes()
        # In-memory warm-shard cache: shard_name -> List[MemoryEntry]
        self._warm_cache: Dict[str, List[MemoryEntry]] = {}

    # -- Tier classification ------------------------------------------------

    def classify_shards(
        self,
        shard_names: List[str],
    ) -> Tuple[List[str], List[str], List[str]]:
        """Classify shard filenames into (hot, warm, cold) lists.

        Parameters
        ----------
        shard_names:
            Sorted list of shard filenames, e.g.
            ``["memories_2026-W01.jsonl", "memories_2026-W09.jsonl", ...]``.

        Returns
        -------
        hot, warm, cold:
            Three lists, oldest-first within each tier.
        """
        now = datetime.now(timezone.utc)
        # Compare at date granularity so a shard whose week started N days ago
        # is consistently classified regardless of the current time-of-day.
        # Without this, a weekly shard (date = Monday 00:00 UTC) falls into
        # warm instead of hot if hot_cutoff lands later than midnight on the
        # same Monday (e.g. hot_days=3, now=Wednesday 10:00 -> cutoff=Sunday
        # 10:00 -> Monday 00:00 < Sunday 10:00 - wrong tier).
        now_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
        hot_cutoff = now_date - timedelta(days=self.hot_days)
        warm_cutoff = now_date - timedelta(days=self.warm_days)

        hot: List[str] = []
        warm: List[str] = []
        cold: List[str] = []

        for name in shard_names:
            shard_dt = self._shard_date(name)
            if shard_dt is None:
                # Unrecognised format - treat as warm (safe default)
                warm.append(name)
            else:
                # Use the END of the shard's week (Monday + 6 days = Sunday)
                # so that a weekly shard is classified hot for its entire week,
                # not just the first hot_days days of it.
                shard_end = shard_dt + timedelta(days=6)
                if shard_end >= hot_cutoff:
                    hot.append(name)
                elif shard_end >= warm_cutoff:
                    warm.append(name)
                else:
                    cold.append(name)

        return hot, warm, cold

    def _shard_date(self, shard_name: str) -> Optional[datetime]:
        """Parse a shard filename and return the Monday of its ISO week.

        E.g. ``memories_2026-W09.jsonl`` -> 2026-02-23 00:00 UTC.
        Returns ``None`` for unrecognised filenames.
        """
        stem = shard_name
        if stem.startswith(self.SHARD_PREFIX):
            stem = stem[len(self.SHARD_PREFIX):]
        if stem.endswith(self.SHARD_SUFFIX):
            stem = stem[: -len(self.SHARD_SUFFIX)]
        # Expect "YYYY-Www"
        try:
            parts = stem.split("-W")
            if len(parts) != 2:
                return None
            year = int(parts[0])
            week = int(parts[1])
            dt = datetime.fromisocalendar(year, week, 1)
            return dt.replace(tzinfo=timezone.utc)
        except (ValueError, AttributeError):
            return None

    # -- Warm tier on-demand load -------------------------------------------

    def load_warm(self, warm_shard_names: List[str]) -> List[MemoryEntry]:
        """Load warm shard files, returning a deduplicated MemoryEntry list.

        Results are cached in ``_warm_cache`` so repeated calls for already-
        loaded shards are O(1).

        Parameters
        ----------
        warm_shard_names:
            List of warm shard filenames to load.

        Returns
        -------
        List of MemoryEntry from all warm shards, deduplicated by hash.
        """
        entries: List[MemoryEntry] = []
        seen: Set[str] = set()

        for shard_name in warm_shard_names:
            if shard_name not in self._warm_cache:
                self._warm_cache[shard_name] = self._load_shard(shard_name)
            for entry in self._warm_cache[shard_name]:
                if entry.hash not in seen:
                    entries.append(entry)
                    seen.add(entry.hash)

        return entries

    def invalidate_warm_cache(self, shard_name: Optional[str] = None) -> None:
        """Clear the warm cache - all shards or a specific one.

        Call after a new entry is written to a warm shard so the next
        warm search sees the updated data.
        """
        if shard_name:
            self._warm_cache.pop(shard_name, None)
        else:
            self._warm_cache.clear()

    # -- Shard hash tracking (incremental sync) -----------------------------

    def shard_hash(self, shard_name: str) -> str:
        """Compute a SHA-256 hash (first 16 hex chars) of a shard file.

        Returns an empty string if the file doesn't exist or can't be read.
        """
        path = os.path.join(self.workspace, shard_name)
        if not os.path.exists(path):
            return ""
        try:
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    h.update(chunk)
            return h.hexdigest()[:16]
        except OSError:
            return ""

    def get_changed_shards(self, shard_names: List[str]) -> List[str]:
        """Return shards whose content has changed since the last snapshot.

        Useful for incremental reload: callers only need to reload changed
        shards rather than all shards.

        Parameters
        ----------
        shard_names:
            All shard filenames to check.

        Returns
        -------
        List of shard filenames whose hash differs from the stored snapshot.
        """
        changed: List[str] = []
        for name in shard_names:
            current = self.shard_hash(name)
            if current and current != self._known_hashes.get(name, ""):
                changed.append(name)
        return changed

    def save_hashes(self, shard_names: List[str]) -> None:
        """Snapshot current file hashes for the given shards.

        Call after a successful load so future startups can use
        ``get_changed_shards()`` for incremental sync.
        """
        for name in shard_names:
            h = self.shard_hash(name)
            if h:
                self._known_hashes[name] = h
        try:
            with open(self._hash_path, "w", encoding="utf-8") as f:
                json.dump(self._known_hashes, f, indent=2)
        except OSError:
            pass  # non-fatal - worst case next startup re-hashes everything

    # -- Tier promotion hint ------------------------------------------------

    def should_promote(self, entry: MemoryEntry, access_count: int) -> bool:
        """Return True if a warm-tier entry warrants promotion to hot.

        Promotion criteria (both must be true):
        - Accessed >= 2 times since last flush.
        - Last accessed within the past 24 hours.

        Parameters
        ----------
        entry:
            The MemoryEntry to evaluate.
        access_count:
            Number of times this entry has been returned from search
            since it entered the warm tier.
        """
        if access_count < 2:
            return False
        last_acc = getattr(entry, "last_accessed", None)
        if not last_acc:
            return False
        try:
            la = datetime.fromisoformat(last_acc.replace("Z", "+00:00"))
            if la.tzinfo is None:
                la = la.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - la) < timedelta(hours=24)
        except Exception:
            return False

    # -- Internal helpers --------------------------------------------------

    def _load_shard(self, shard_name: str) -> List[MemoryEntry]:
        """Load all valid entries from one JSONL shard file.

        Corrupted lines are silently skipped - a partial file is always
        better than a crash.
        """
        path = os.path.join(self.workspace, shard_name)
        if not os.path.exists(path):
            return []
        entries: List[MemoryEntry] = []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        entries.append(MemoryEntry.from_dict(json.loads(stripped)))
                    except Exception:
                        pass  # corrupted line - skip safely
        except OSError:
            pass
        return entries

    def _load_hashes(self) -> Dict[str, str]:
        """Load previously snapshotted shard hashes from disk."""
        if not os.path.exists(self._hash_path):
            return {}
        try:
            with open(self._hash_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {
                k: v
                for k, v in data.items()
                if isinstance(k, str) and isinstance(v, str)
            }
        except (OSError, json.JSONDecodeError):
            return {}
