"""
WAL (Write-Ahead Log) for the Parsica Spirit engine - Incremental Index Loading.

Provides a simple, standalone WAL separate from the WALManager
in performance.py.  This WAL is specifically for tracking MemoryEntry dicts
that have been ingested but not yet folded into a saved BM25 index snapshot.

Usage::

    writer = WALWriter()
    writer.open("/path/to/.parsica_wal.jsonl")
    writer.append(entry.to_dict())
    # ... many more appends ...
    writer.close()

    reader = WALReader()
    reader.open("/path/to/.parsica_wal.jsonl")
    entries = reader.read_all()    # List[dict]
    reader.clear()                 # removes the file after successful flush
"""

import json
import os
from typing import List, Optional


class WALWriter:
    """Append-only WAL writer for serialised MemoryEntry dicts.

    Each call to ``append()`` writes one JSON line to the file and
    immediately flushes the OS buffer so data survives a crash.

    Call ``open()`` before any ``append()`` or ``size()`` call.
    Call ``close()`` when done (also called by ``__del__``).
    """

    def __init__(self) -> None:
        self._path: Optional[str] = None
        self._fh = None

    def open(self, path: str) -> None:
        """Open (or create) the WAL file for appending."""
        self._path = path
        # Ensure parent directory exists
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        newly = not os.path.exists(path)
        self._fh = open(path, "a", encoding="utf-8")
        if newly:
            try:
                os.chmod(path, 0o600)   # WAL content private to the owning user, like the shards (best-effort)
            except OSError:
                pass

    def append(self, entry_dict: dict) -> None:
        """Append one entry dict as a JSON line.

        Args:
            entry_dict: Serialised MemoryEntry (from ``entry.to_dict()``).

        Raises:
            RuntimeError: If ``open()`` has not been called.
        """
        if self._fh is None:
            raise RuntimeError("WALWriter.open() must be called before append()")
        line = json.dumps(entry_dict, ensure_ascii=False) + "\n"
        self._fh.write(line)
        self._fh.flush()

    def close(self) -> None:
        """Flush and close the underlying file handle."""
        if self._fh is not None:
            try:
                self._fh.flush()
                self._fh.close()
            except OSError:
                pass
            finally:
                self._fh = None

    def size(self) -> int:
        """Return current WAL file size in bytes (0 if not yet created)."""
        if self._path and os.path.exists(self._path):
            return os.path.getsize(self._path)
        return 0

    def __del__(self) -> None:  # pragma: no cover
        self.close()


class WALReader:
    """Reader for WAL files produced by :class:`WALWriter`.

    Call ``open()`` before ``read_all()`` or ``clear()``.
    """

    def __init__(self) -> None:
        self._path: Optional[str] = None

    def open(self, path: str) -> None:
        """Point the reader at a WAL file (need not exist yet)."""
        self._path = path

    def read_all(self) -> List[dict]:
        """Return all valid entries from the WAL.

        Partial / corrupted JSON lines are silently skipped so a crash
        mid-write never prevents startup.

        Returns:
            List of entry dicts.  Empty list if file does not exist.
        """
        if not self._path or not os.path.exists(self._path):
            return []

        entries: List[dict] = []
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                for line in fh:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        entries.append(json.loads(stripped))
                    except json.JSONDecodeError:
                        pass  # corrupted line - skip safely
        except OSError:
            pass
        return entries

    def clear(self) -> None:
        """Delete the WAL file after a successful flush/rebuild.

        Safe to call even if the file does not exist.
        """
        if self._path and os.path.exists(self._path):
            try:
                os.remove(self._path)
            except OSError:
                pass
