"""
Time-Window Shard Manager for the Parsica Spirit engine.

Splits the memory store into 7-day rolling shards so startup only
loads recent memories.  Each shard is a JSONL file named after the
ISO calendar week it covers::

    memories_2026-W09.jsonl
    memories_2026-W10.jsonl   <- current week
    ...

Usage::

    mgr = ShardManager("/path/to/workspace")
    mgr.write_entry(entry)                    # routes by entry.created
    recent = mgr.load_active_shards(weeks=1)  # this week + last week
    all_   = mgr.load_all_shards()
    names  = mgr.list_shards()                # sorted filenames
"""

import json
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from .entry import MemoryEntry


class ShardManager:
    """Manages weekly time-window shard files.

    This class is distinct from :class:`parsica_spirit.engine.sharding.ShardManager`
    (the date/topic shard system).  The two coexist and serve
    different purposes:

    * ``sharding.ShardManager``  - ``shard_YYYY-MM_topic.json`` files,
      with a full index and topic routing.
    * ``shards.ShardManager``    - ``memories_YYYY-WXX.jsonl`` files,
      simple append-only weekly rolling windows.

    Parameters
    ----------
    workspace:
        Root directory.  Shard files are stored directly inside it.
    """

    SHARD_PREFIX = "memories_"
    SHARD_SUFFIX = ".jsonl"

    def __init__(self, workspace: str) -> None:
        self.workspace = workspace
        os.makedirs(workspace, exist_ok=True)
        try:
            os.chmod(workspace, 0o700)   # store dir private to the owning user (best-effort; no-op on Windows)
        except OSError:
            pass

    # -- shard naming -----------------------------------------------------

    def _shard_name(self, dt: datetime) -> str:
        """Return the shard filename for the given datetime."""
        year, week, _ = dt.isocalendar()
        return f"{self.SHARD_PREFIX}{year}-W{week:02d}{self.SHARD_SUFFIX}"

    def _active_shard_name(self) -> str:
        """Return the shard filename for the current week (UTC)."""
        return self._shard_name(datetime.now(timezone.utc))

    def _shard_path(self, shard_name: str) -> str:
        return os.path.join(self.workspace, shard_name)

    # -- write -------------------------------------------------------------

    def write_entry(self, entry: MemoryEntry) -> None:
        """Append *entry* to the appropriate weekly shard.

        Routing rules:
        * ``entry.created`` is a valid ISO datetime -> route to its calendar week.
        * ``entry.created`` is ``None`` or unparseable -> route to active shard.

        The file is opened in append mode on every call (safe for concurrent
        multi-process writes to different shards; within one process the GIL
        protects same-file appends).
        """
        dt = self._parse_ts(entry.created)
        shard_name = self._shard_name(dt)
        shard_path = self._shard_path(shard_name)
        line = json.dumps(entry.to_dict(), ensure_ascii=False) + "\n"
        newly = not os.path.exists(shard_path)
        with open(shard_path, "a", encoding="utf-8") as fh:
            fh.write(line)
        if newly:
            try:
                os.chmod(shard_path, 0o600)   # shard content private to the owning user (best-effort)
            except OSError:
                pass

    # -- read --------------------------------------------------------------

    def load_active_shards(self, weeks: int = 1) -> List[MemoryEntry]:
        """Load memories from the current week plus the past *weeks* weeks.

        Deduplicates by hash.

        Parameters
        ----------
        weeks:
            How many past weeks to include in addition to the current week.
            Default ``1`` -> current week + previous week (~=2 weeks of data).
        """
        now = datetime.now(timezone.utc)
        entries: List[MemoryEntry] = []
        seen_hashes: set = set()

        for w in range(weeks + 1):
            dt = now - timedelta(weeks=w)
            shard_name = self._shard_name(dt)
            for entry in self._load_shard(shard_name):
                if entry.hash not in seen_hashes:
                    entries.append(entry)
                    seen_hashes.add(entry.hash)

        return entries

    def load_all_shards(self) -> List[MemoryEntry]:
        """Load memories from every shard file found in the workspace.

        Deduplicates by hash.  Shards are processed in chronological order
        (oldest first) so the most-recent version of a hash wins.
        """
        entries: List[MemoryEntry] = []
        seen_hashes: set = set()

        for shard_name in self.list_shards():
            for entry in self._load_shard(shard_name):
                if entry.hash not in seen_hashes:
                    entries.append(entry)
                    seen_hashes.add(entry.hash)

        return entries

    def list_shards(self) -> List[str]:
        """Return sorted list of shard filenames present in the workspace."""
        try:
            files = [
                f for f in os.listdir(self.workspace)
                if f.startswith(self.SHARD_PREFIX) and f.endswith(self.SHARD_SUFFIX)
            ]
            return sorted(files)
        except FileNotFoundError:
            return []

    # -- rewrite -----------------------------------------------------------

    def rewrite_shards(self, entries: List[MemoryEntry]) -> List[str]:
        """Rewrite all weekly shard files from the provided live entries."""
        shard_groups = {}
        for entry in entries:
            shard_name = self._shard_name(self._parse_ts(entry.created))
            shard_groups.setdefault(shard_name, []).append(entry)

        written: List[str] = []
        for shard_name, group in shard_groups.items():
            shard_path = self._shard_path(shard_name)
            try:
                with open(shard_path, "w", encoding="utf-8") as fh:
                    for entry in group:
                        fh.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
                written.append(shard_name)
            except OSError as e:
                import sys
                print(f"[parsica-spirit engine] WARNING: failed to write shard {shard_name}: {e}", file=sys.stderr)

        for existing_name in self.list_shards():
            if existing_name not in shard_groups:
                try:
                    os.remove(self._shard_path(existing_name))
                except OSError as e:
                    import sys
                    print(f"[parsica-spirit engine] WARNING: failed to remove stale shard {existing_name}: {e}", file=sys.stderr)

        return written

    def rewrite_entries(
        self,
        entries: List[MemoryEntry],
        protected_shards: Optional[set] = None,
    ) -> List[str]:
        """Rewrite weekly shard files, leaving protected shards untouched."""
        protected_shards = protected_shards or set()

        shard_groups = {}
        for entry in entries:
            shard_name = self._shard_name(self._parse_ts(entry.created))
            shard_groups.setdefault(shard_name, []).append(entry)

        written: List[str] = []
        for shard_name, group in shard_groups.items():
            if shard_name in protected_shards:
                continue
            shard_path = self._shard_path(shard_name)
            try:
                with open(shard_path, "w", encoding="utf-8") as fh:
                    for entry in group:
                        fh.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
                written.append(shard_name)
            except OSError as e:
                import sys
                print(f"[parsica-spirit engine] WARNING: failed to write shard {shard_name}: {e}", file=sys.stderr)

        for existing_name in self.list_shards():
            if existing_name not in shard_groups and existing_name not in protected_shards:
                try:
                    os.remove(self._shard_path(existing_name))
                except OSError as e:
                    import sys
                    print(f"[parsica-spirit engine] WARNING: failed to remove stale shard {existing_name}: {e}", file=sys.stderr)

        return written

    # -- internals ---------------------------------------------------------

    def _load_shard(self, shard_name: str) -> List[MemoryEntry]:
        """Load all valid entries from one shard file.

        Corrupted lines are silently skipped.
        """
        shard_path = self._shard_path(shard_name)
        if not os.path.exists(shard_path):
            return []

        entries: List[MemoryEntry] = []
        try:
            with open(shard_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        entries.append(MemoryEntry.from_dict(json.loads(stripped)))
                    except Exception:
                        pass  # corrupted line - skip
        except OSError:
            pass
        return entries

    @staticmethod
    def _parse_ts(ts: Optional[str]) -> datetime:
        """Parse an ISO timestamp string into a timezone-aware datetime.

        Falls back to ``datetime.now(UTC)`` on any error or ``None`` input.
        """
        if not ts:
            return datetime.now(timezone.utc)
        try:
            # Handle both "2026-02-15T..." and "2026-02-15T...+00:00" etc.
            normalized = ts.replace("Z", "+00:00")
            dt = datetime.fromisoformat(normalized)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, AttributeError):
            return datetime.now(timezone.utc)
