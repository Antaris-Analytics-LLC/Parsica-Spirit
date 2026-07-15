"""Ingest manifest for tracking source provenance and deduplication."""

import json
import logging
import os
import shutil
import time
from typing import Dict, Optional, Set

_log = logging.getLogger("parsica_spirit.engine.ingest_manifest")


class IngestManifest:
    """Track ingested sources for incremental re-ingestion and dedup.

    Manifest file format::

        {
            "sources": {
                "<source_url>": {
                    "last_crawl_ts": float,
                    "last_hash": str,
                    "entry_ids": [str, ...],
                    "content_hashes": [str, ...]
                }
            }
        }
    """

    def __init__(self, workspace: str):
        self.workspace = os.path.abspath(workspace)
        self._path = os.path.join(self.workspace, "ingest_manifest.json")
        self._data: Dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        """Load manifest from disk with corruption recovery."""
        if not os.path.exists(self._path):
            self._data = {}
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                raw = json.load(f)
            self._data = raw.get("sources", {}) if isinstance(raw, dict) else {}
        except (json.JSONDecodeError, OSError) as e:
            _log.warning("Corrupt manifest, recovering: %s", e)
            # Try backup
            backup = self._path + ".bak"
            if os.path.exists(backup):
                try:
                    with open(backup, encoding="utf-8") as f:
                        raw = json.load(f)
                    self._data = raw.get("sources", {}) if isinstance(raw, dict) else {}
                    _log.info("Recovered manifest from backup")
                    return
                except Exception:
                    pass
            self._data = {}

    def save(self) -> None:
        """Atomic JSON write with backup."""
        os.makedirs(self.workspace, exist_ok=True)
        try:
            os.chmod(self.workspace, 0o700)   # store dir private to the owning user (best-effort; no-op on Windows)
        except OSError:
            pass
        tmp_path = self._path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump({"sources": self._data}, f, indent=2)
            # Backup current before overwrite
            if os.path.exists(self._path):
                shutil.copy2(self._path, self._path + ".bak")
            os.replace(tmp_path, self._path)
            try:
                os.chmod(self._path, 0o600)   # manifest content private to the owning user (best-effort)
            except OSError:
                pass
        except Exception as e:
            _log.error("Failed to save manifest: %s", e)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def get(self, source_url: str) -> Optional[dict]:
        """Get manifest entry for a source URL."""
        entry = self._data.get(source_url)
        if entry is None:
            return None
        return {
            "last_crawl_ts": entry.get("last_crawl_ts"),
            "last_hash": entry.get("last_hash"),
            "entry_ids": entry.get("entry_ids", []),
        }

    def update(self, source_url: str, content_hash: str, entry_ids: list) -> None:
        """Upsert a source entry in the manifest.

        Args:
            source_url: The source URL being tracked.
            content_hash: Hash of the most recently ingested chunk (used as
                the canonical "last seen" marker).
            entry_ids: All chunk hashes from this crawl/ingest. These are
                accumulated into content_hashes so get_known_hashes() can
                skip all previously seen chunks on re-ingestion.
        """
        existing = self._data.get(source_url, {})
        existing_hashes = set(existing.get("content_hashes", []))
        # Accumulate all entry_ids as known hashes (complete dedup coverage)
        existing_hashes.add(content_hash)
        existing_hashes.update(entry_ids)
        existing_entry_ids = set(existing.get("entry_ids", []))
        existing_entry_ids.update(entry_ids)

        self._data[source_url] = {
            "last_crawl_ts": time.time(),
            "last_hash": content_hash,
            "entry_ids": list(existing_entry_ids),
            "content_hashes": list(existing_hashes),
        }

    def remove(self, source_url: str) -> None:
        """Remove a source from the manifest."""
        self._data.pop(source_url, None)

    def get_known_hashes(self, source_url: str) -> Set[str]:
        """Get all known content hashes for a source (for dedup)."""
        entry = self._data.get(source_url, {})
        return set(entry.get("content_hashes", []))
