"""
MemoryManager - batteries-included convenience layer for Parsica-Memory.

Provides per-user memory isolation, automatic noise filtering, sensible
cross-session defaults, and optional enrichment via an injected callable.

Usage::

    from parsica_spirit.engine import MemoryManager

    # Basic - no enrichment (BM25-only, still good)
    mm = MemoryManager("./store")

    # With enrichment - inject any callable: str -> {tags, summary, keywords}
    mm = MemoryManager("./store", enricher=my_enricher)

    # Ingest a conversation turn
    mm.ingest("user123", "What's the weather?", "It's sunny in LA.")

    # Recall relevant memories
    results = mm.recall("user123", "weather")

    # Enrich all existing memories (backfill)
    mm.enrich_all("user123")

    # Export user memories to JSON
    mm.export("user123", "./export.json")

    # Stats
    mm.stats("user123")
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

try:
    from .core import MemorySystem
except ImportError:
    from parsica_spirit.engine.core import MemorySystem

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Hardening - Safe charset for user_id validation
# Only alphanumeric, hyphen, underscore, and dot are allowed.
# This pattern is used by _user_path() to REJECT invalid IDs outright
# rather than silently sanitizing them (lossy rewriting creates collisions:
# e.g. "a/b" and "ab" would map to the same user).
# --------------------------------------------------------------------------
_SAFE_USER_ID_RE = re.compile(r'^[a-zA-Z0-9._-]+$')


class MemoryManager:
    """Batteries-included memory manager with per-user isolation.

    Each user gets their own ``MemorySystem`` backed by a sub-directory,
    keeping memories fully isolated.

    Args:
        store_path: Root directory for all user memory stores.
        search_limit: Max results per recall/search (default 10).
        min_relevance: Minimum relevance score 0-1 (default 0.3).
        enricher: Optional enrichment callable (str -> {tags, summary, keywords}).
            The engine makes no network calls; any LLM/provider wiring lives in the
            caller and is injected here.
        agent_name: Name used in memory provenance (default "parsica").
        storage_mode: Storage backend mode - "legacy", "sharded", or
            "tiered_storage".  Alias "tiered" is accepted.
    """

    # ------------------------------------------------------------------
    # Hardening - Storage mode mapping table
    #
    # Maps canonical mode names to (use_sharding, time_sharding, tiered_storage)
    # tuples.  ALL callers go through _normalize_storage_mode() to resolve
    # aliases and validate - no string branching elsewhere.
    # ------------------------------------------------------------------
    _STORAGE_MODE_FLAGS: Dict[str, tuple] = {
        "sharded":         (True,  False, False),
        "tiered_storage":  (False, True,  True),
        "legacy":          (False, False, False),
    }

    # Alias mapping: convenience names -> canonical names
    _STORAGE_MODE_ALIASES: Dict[str, str] = {
        "tiered":  "tiered_storage",
    }

    @classmethod
    def _normalize_storage_mode(cls, mode: Optional[str]) -> Optional[str]:
        """Resolve aliases and validate a storage_mode string.

        Returns the canonical mode name, or None if *mode* is None.
        Raises ValueError for unrecognised mode names.

        Hardening: fully centralized normalization.
        Every caller that needs a mode string goes through here.
        """
        if mode is None:
            return None
        canonical = cls._STORAGE_MODE_ALIASES.get(mode, mode)
        if canonical not in cls._STORAGE_MODE_FLAGS:
            raise ValueError(
                f"Unsupported storage_mode {mode!r}. "
                f"Valid values: {sorted(cls._STORAGE_MODE_FLAGS)} "
                f"(aliases: {sorted(cls._STORAGE_MODE_ALIASES)})"
            )
        return canonical

    def __init__(
        self,
        store_path: str,
        search_limit: int = 10,
        min_relevance: float = 0.3,
        enricher=None,
        agent_name: str = "parsica",
        storage_mode: Optional[str] = None,
    ):
        self.store_path = Path(store_path)
        self.store_path.mkdir(parents=True, exist_ok=True)
        self.search_limit = search_limit
        self.min_relevance = min_relevance
        self.agent_name = agent_name
        self.storage_mode = storage_mode
        self._stores: Dict[str, MemorySystem] = {}
        self._enricher = enricher

        # Hardening: normalise and store the canonical mode.
        # _normalize_storage_mode() resolves aliases ("tiered" ->
        # "tiered_storage") and raises on invalid values.
        self._storage_mode: Optional[str] = self._normalize_storage_mode(storage_mode)

    # ------------------------------------------------------------------
    # Path containment  (Hardening)
    # ------------------------------------------------------------------

    def _user_path(self, user_id: str) -> Path:
        """Return the resolved path for a user store, enforcing containment.

        Hardening:
        1. Validate user_id against safe charset (alphanumeric, hyphen,
           underscore, dot).
        2. **REJECT invalid input with ValueError** - do NOT silently
           sanitize.  Lossy rewriting creates collisions (e.g. ``a/b`` and
           ``ab`` would become the same user).
        3. Resolve path and assert it stays under store root.

        Raises:
            ValueError: If user_id contains unsafe characters or if the
                resolved path escapes the store root.
        """
        # Step 1: reject any user_id that contains characters outside the
        # safe set.  This catches path traversal (../, /), null bytes,
        # and other dangerous input BEFORE touching the filesystem.
        if not user_id or not _SAFE_USER_ID_RE.match(user_id) or user_id in ('.', '..'):
            raise ValueError(
                f"Invalid user_id {user_id!r}: must contain only "
                "alphanumeric characters, hyphens, underscores, or dots"
            )

        # Step 2: resolve and verify containment under store root.
        candidate = (self.store_path / user_id).resolve()
        store_root = self.store_path.resolve()
        try:
            candidate.relative_to(store_root)
        except ValueError:
            raise ValueError(
                f"user_id {user_id!r} resolves to a path outside the "
                f"store root: {candidate}"
            )
        return candidate

    # ------------------------------------------------------------------
    # Store management
    # ------------------------------------------------------------------

    def _get_store(self, user_id: str) -> MemorySystem:
        """Get or create a MemorySystem for a specific user.

        Hardening changes:
        - uses _user_path() for path containment.
        - passes 3 shard params from _STORAGE_MODE_FLAGS table
          via the centralised _normalize_storage_mode() path.
        - fail-closed on load errors (raise on corruption,
          silent only for FileNotFoundError).
        """
        if user_id not in self._stores:
            # path containment - validates and resolves safely
            user_path = self._user_path(user_id)
            user_path.mkdir(parents=True, exist_ok=True)

            # derive shard kwargs from the canonical storage mode.
            # _storage_mode is already normalised (aliases resolved) by __init__.
            shard_kwargs = {}
            if self._storage_mode is not None:
                use_s, time_s, tier_s = self._STORAGE_MODE_FLAGS[self._storage_mode]
                shard_kwargs = {
                    "use_sharding": use_s,
                    "sharding": time_s,
                    "tiered_storage": tier_s,
                }

            store = MemorySystem(
                workspace=str(user_path),
                agent_name=self.agent_name,
                enricher=self._enricher,
                **shard_kwargs,
            )

            # load failure handling - fail closed.
            # FileNotFoundError is expected for new users -> silently create
            # an empty store.  ANY other failure on an existing path means
            # potential corruption -> warn to stderr AND raise so callers
            # don't silently proceed with an empty store.
            try:
                store.load()
            except FileNotFoundError:
                pass  # First use - no data yet, expected
            except Exception as exc:
                print(
                    f"[parsica-spirit engine] FATAL: failed to load store for "
                    f"user {user_id!r}: {exc}",
                    file=sys.stderr,
                )
                raise  # Fail closed - do NOT silently proceed with empty store

            self._stores[user_id] = store
        return self._stores[user_id]

    # ------------------------------------------------------------------
    # Noise filter
    # ------------------------------------------------------------------

    @staticmethod
    def _should_skip(text: str) -> bool:
        """Return True if text is noise that shouldn't be stored."""
        if not text or not text.strip():
            return True
        stripped = text.strip()
        if len(stripped) < 20:
            return True
        if not re.search(r"[a-zA-Z0-9]", stripped):
            return True
        # Common framework noise
        noise_prefixes = (
            "### Relevant Context",
        )
        for prefix in noise_prefixes:
            if stripped.startswith(prefix):
                return True
        return False

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def ingest(
        self,
        user_id: str,
        user_message: str,
        assistant_response: str,
        session_id: str = "",
        source: str = "conversation",
    ) -> bool:
        """Store a conversation turn in memory.

        Skips noise automatically. Enriches at ingest time if an enricher
        is configured. Saves to disk immediately.

        Returns True if ingested, False if skipped.
        """
        if self._should_skip(user_message) or self._should_skip(assistant_response):
            return False
        try:
            store = self._get_store(user_id)
            content = f"User: {user_message}\nAssistant: {assistant_response}"
            kwargs = {"source": source, "category": "chat"}
            if session_id:
                kwargs["session_id"] = session_id
            store.ingest(content, **kwargs)
            store.save()
            return True
        except Exception as e:
            logger.warning("Memory ingest failed: %s", e)
            return False

    def ingest_raw(
        self,
        user_id: str,
        content: str,
        source: str = "manual",
        category: str = "note",
        session_id: str = "",
        **kwargs,
    ) -> bool:
        """Ingest arbitrary content (not a conversation turn).

        Use for documents, notes, facts, or anything that isn't a
        user/bot exchange.
        """
        if self._should_skip(content):
            return False
        try:
            store = self._get_store(user_id)
            ingest_kwargs = {"source": source, "category": category}
            if session_id:
                ingest_kwargs["session_id"] = session_id
            ingest_kwargs.update(kwargs)
            store.ingest(content, **ingest_kwargs)
            store.save()
            return True
        except Exception as e:
            logger.warning("Raw ingest failed: %s", e)
            return False

    def recall(
        self,
        user_id: str,
        query: str,
        session_id: str = "",
        limit: Optional[int] = None,
        confidence_floor: float = 0.0,
    ) -> List[str]:
        """Recall relevant memories as a list of content strings.

        Uses cross-session semantic recall by default - facts and decisions
        cross session boundaries, episodic chatter stays scoped.
        """
        try:
            store = self._get_store(user_id)
            search_kwargs = {
                "limit": limit or self.search_limit,
                "use_decay": True,
                "cross_session_recall": "semantic",
                "confidence_floor": confidence_floor,
            }
            if session_id:
                search_kwargs["session_id"] = session_id
            results = store.search(query, **search_kwargs)
            return [
                r.content
                for r in results
                if self._get_score(r) >= self.min_relevance
            ]
        except Exception as e:
            logger.warning("Memory recall failed: %s", e)
            return []

    def recall_structured(
        self,
        user_id: str,
        query: str,
        session_id: str = "",
        limit: Optional[int] = None,
        confidence_floor: float = 0.0,
    ) -> List[dict]:
        """Recall with full provenance: {content, provenance, confidence, source, memory_type}"""
        try:
            store = self._get_store(user_id)
            search_kwargs = {
                "limit": limit or self.search_limit,
                "use_decay": True,
                "cross_session_recall": "semantic",
                "confidence_floor": confidence_floor,
            }
            if session_id:
                search_kwargs["session_id"] = session_id
            results = store.search(query, **search_kwargs)
            return [
                {
                    "content": r.content,
                    "provenance": getattr(r, "provenance", "self"),
                    "confidence": getattr(r, "confidence", 0.5),
                    "source": getattr(r, "source", ""),
                    "memory_type": getattr(r, "memory_type", "episodic"),
                    "parent_hash": getattr(r, "parent_hash", ""),
                    "hash": getattr(r, "hash", ""),
                }
                for r in results
                if self._get_score(r) >= self.min_relevance
            ]
        except Exception as e:
            logger.warning("Structured memory recall failed: %s", e)
            return []

    def search(
        self,
        user_id: str,
        query: str,
        session_id: str = "",
        limit: Optional[int] = None,
        explain: bool = False,
        confidence_floor: float = 0.0,
    ) -> List[dict]:
        """Search memories with full metadata.

        Returns list of dicts with content, relevance, source, category,
        matched_terms, and optionally explanation.
        """
        try:
            store = self._get_store(user_id)
            search_kwargs = {
                "limit": limit or self.search_limit,
                "use_decay": True,
                "explain": explain,
                "cross_session_recall": "semantic",
                "confidence_floor": confidence_floor,
            }
            if session_id:
                search_kwargs["session_id"] = session_id
            results = store.search(query, **search_kwargs)
            return [
                {
                    "content": r.content,
                    "relevance": self._get_score(r),
                    "source": getattr(r, "source", ""),
                    "category": getattr(r, "category", ""),
                    "matched_terms": getattr(r, "matched_terms", []),
                    "explanation": getattr(r, "explanation", "") if explain else "",
                }
                for r in results
            ]
        except Exception as e:
            logger.warning("Memory search failed: %s", e)
            return []

    # ------------------------------------------------------------------
    # Enrichment
    # ------------------------------------------------------------------

    def enrich_all(
        self,
        user_id: str,
        overwrite: bool = False,
        progress_fn=None,
    ) -> int:
        """Batch-enrich all existing memories for a user.

        Requires an enricher to be configured. Skips already-enriched
        entries unless overwrite=True.

        Returns count of entries enriched.
        """
        if self._enricher is None:
            logger.warning("No enricher configured - pass an enricher callable to MemoryManager(enricher=...)")
            return 0
        try:
            store = self._get_store(user_id)
            return store.re_enrich(overwrite=overwrite, progress_fn=progress_fn)
        except Exception as e:
            logger.warning("Batch enrichment failed: %s", e)
            return 0

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def stats(self, user_id: Optional[str] = None) -> dict:
        """Memory stats for a user or aggregate across all users.

        Hardening: aggregate stats now discover ALL user
        subdirectories from disk (not only loaded in-memory stores), so
        users whose stores have not been accessed this session are still
        counted.  The total is the union of in-memory stores + disk dirs.
        """
        if user_id is not None:
            try:
                store = self._get_store(user_id)
                s = store.stats()
                # use _user_path() for path containment
                user_path = self._user_path(user_id)
                size = sum(f.stat().st_size for f in user_path.rglob("*") if f.is_file())
                files = [f for f in user_path.rglob("*") if f.is_file()]
                oldest = newest = None
                if files:
                    mtimes = sorted(f.stat().st_mtime for f in files)
                    oldest = datetime.fromtimestamp(mtimes[0], tz=timezone.utc).isoformat()
                    newest = datetime.fromtimestamp(mtimes[-1], tz=timezone.utc).isoformat()
                return {
                    "user_id": user_id,
                    "entry_count": s.get("total", 0),
                    "store_size_bytes": size,
                    "oldest_entry": oldest,
                    "newest_entry": newest,
                    "enricher_active": self._enricher is not None,
                }
            except Exception as e:
                return {"user_id": user_id, "error": str(e)}

        # discover all users - union of in-memory stores AND
        # subdirectories on disk.  This ensures users whose stores were
        # never loaded in this session are still counted.
        disk_user_ids: set = set()
        try:
            for entry in self.store_path.iterdir():
                if entry.is_dir():
                    disk_user_ids.add(entry.name)
        except Exception:
            pass

        all_user_ids = set(self._stores.keys()) | disk_user_ids

        total = 0
        per_user = {}
        for uid in all_user_ids:
            try:
                if uid in self._stores:
                    s = self._stores[uid].stats()
                    c = s.get("total", 0)
                else:
                    # User exists on disk but not loaded - load to get count
                    try:
                        s = self._get_store(uid).stats()
                        c = s.get("total", 0)
                    except Exception:
                        c = 0
                per_user[uid] = c
                total += c
            except Exception:
                per_user[uid] = 0

        unloaded_count = len(disk_user_ids - set(self._stores.keys()))
        return {
            "user_count": len(all_user_ids),
            "total_entries": total,
            "unloaded_user_count": unloaded_count,
            "per_user": per_user,
            "enricher_active": self._enricher is not None,
        }

    def export(self, user_id: str, output_path: str) -> int:
        """Export all memories for a user to a JSON file.

        Uses ``store.memories`` directly instead of searching with
        an empty query string (which returns no results from BM25).  This
        ensures all memories are exported regardless of search index state.

        Returns count of exported entries.
        """
        try:
            store = self._get_store(user_id)
            now = datetime.now(tz=timezone.utc)
            entries = [
                {
                    "content": getattr(m, "content", ""),
                    "score": store.decay.score(m, now),
                    "timestamp": getattr(m, "created", None),
                    "source": getattr(m, "source", ""),
                    "category": getattr(m, "category", ""),
                    "memory_type": getattr(m, "memory_type", "episodic"),
                }
                for m in store.memories
            ]
            data = {
                "user_id": user_id,
                "exported_at": now.isoformat(),
                "entry_count": len(entries),
                "entries": entries,
            }
            out = Path(output_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(data, indent=2, default=str))
            return len(entries)
        except Exception as e:
            logger.warning("Export failed: %s", e)
            return 0

    def clear(self, user_id: str) -> int:
        """Delete all memory files for a user. Returns count of deleted files."""
        try:
            # path containment via _user_path()
            user_path = self._user_path(user_id)
            if not user_path.exists():
                return 0
            files = [f for f in user_path.rglob("*") if f.is_file()]
            count = len(files)
            for f in files:
                f.unlink()
            for d in sorted(user_path.rglob("*"), reverse=True):
                if d.is_dir() and d != user_path:
                    try:
                        d.rmdir()
                    except OSError:
                        pass
            self._stores.pop(user_id, None)
            return count
        except Exception as e:
            logger.warning("Clear failed: %s", e)
            return 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_score(result) -> float:
        """Extract relevance score from a search result."""
        for attr in ("relevance", "score", "confidence"):
            val = getattr(result, attr, None)
            if val is not None:
                return float(val)
        return 1.0
