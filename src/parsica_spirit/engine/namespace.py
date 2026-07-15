"""
Namespace Isolation for the Parsica Spirit engine.

Namespaces provide fully isolated memory spaces.  Search in one namespace
never returns results from another.  Each namespace has its own workspace
directory, shards, and indexes.

API
---
::

    # Proxy access (lazy-creates the namespace)
    ns = memory.namespace("project-alpha")
    ns.ingest("Some project-specific memory")
    results = ns.search("query")

    # Lifecycle management
    memory.create_namespace("project-alpha")
    memory.archive_namespace("old-project")
    memory.delete_namespace("temp-project")
    memory.list_namespaces()

Design
------
* Each namespace lives in ``{workspace}/namespaces/{name}/``.
* A manifest file at ``{workspace}/namespace_manifest.json`` tracks status.
* ``NamespacedMemory`` is a thin proxy that forwards all calls to its own
  ``MemorySystem`` instance with the per-namespace workspace.
* The default namespace (``"default"``) maintains full backward compatibility.
* Namespace names must be non-empty slugs (alphanumeric, hyphens, underscores).
"""

import json
import os
import re
import shutil
from datetime import datetime
from typing import Dict, List, Optional

# Regex for valid namespace names
_NAME_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$')

_MANIFEST_FILENAME = "namespace_manifest.json"

_STATUS_ACTIVE = "active"
_STATUS_ARCHIVED = "archived"


# -- helpers -------------------------------------------------------------------

def _validate_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise ValueError(
            f"Invalid namespace name {name!r}. "
            "Use alphanumeric characters, hyphens, or underscores. "
            "Must start with alphanumeric (1-64 chars)."
        )


class NamespaceManifest:
    """Reads and writes the namespace manifest file.

    Manifest structure::

        {
          "version": "1.0",
          "updated_at": "<iso>",
          "namespaces": {
            "default": {
              "status": "active",
              "created_at": "<iso>",
              "archived_at": null
            },
            ...
          }
        }
    """

    def __init__(self, workspace: str):
        self.path = os.path.join(workspace, _MANIFEST_FILENAME)
        self._data: Dict = {}
        self._load()

    # -- I/O ------------------------------------------------------------------

    def _load(self) -> None:
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                self._data = json.load(f)
        else:
            self._data = {
                "version": "1.0",
                "updated_at": datetime.now().isoformat(),
                "namespaces": {},
            }

    def _save(self) -> None:
        self._data["updated_at"] = datetime.now().isoformat()
        from .utils import atomic_write_json
        atomic_write_json(self.path, self._data)

    # -- namespace CRUD -------------------------------------------------------

    def exists(self, name: str) -> bool:
        return name in self._data.get("namespaces", {})

    def get(self, name: str) -> Optional[Dict]:
        return self._data.get("namespaces", {}).get(name)

    def all(self) -> Dict[str, Dict]:
        return dict(self._data.get("namespaces", {}))

    def create(self, name: str) -> None:
        """Register a new namespace.  No-ops if already active."""
        ns = self._data.setdefault("namespaces", {}).get(name)
        if ns and ns.get("status") == _STATUS_ACTIVE:
            return  # Already exists and active
        self._data["namespaces"][name] = {
            "status": _STATUS_ACTIVE,
            "created_at": datetime.now().isoformat(),
            "archived_at": None,
        }
        self._save()

    def archive(self, name: str) -> None:
        ns = self._data.get("namespaces", {}).get(name)
        if not ns:
            raise KeyError(f"Namespace {name!r} does not exist.")
        ns["status"] = _STATUS_ARCHIVED
        ns["archived_at"] = datetime.now().isoformat()
        self._save()

    def delete(self, name: str) -> None:
        namespaces = self._data.get("namespaces", {})
        if name not in namespaces:
            raise KeyError(f"Namespace {name!r} does not exist.")
        del namespaces[name]
        self._save()

    def list_active(self) -> List[str]:
        return [
            n for n, info in self._data.get("namespaces", {}).items()
            if info.get("status") == _STATUS_ACTIVE
        ]

    def list_all(self) -> List[Dict]:
        result = []
        for name, info in self._data.get("namespaces", {}).items():
            result.append({"name": name, **info})
        return result


# -- NamespacedMemory ----------------------------------------------------------

class NamespacedMemory:
    """A proxy that exposes the full MemorySystem API for a specific namespace.

    All operations are scoped to the namespace's own workspace directory.
    Searching in this namespace returns *only* memories from this namespace.

    Do not instantiate directly - use ``memory.namespace("name")``.
    """

    def __init__(self, name: str, workspace: str, half_life: float = 7.0,
                 tag_terms: List[str] = None):
        self.name = name
        self.workspace = workspace
        # Lazy-import to avoid circular dependency at module load
        from .core import MemorySystem
        self._system = MemorySystem(
            workspace=workspace,
            half_life=half_life,
            tag_terms=tag_terms,
        )
        self._loaded = False

    def _ensure_loaded(self):
        """Lazy-load memories from disk on first access."""
        if not self._loaded:
            self._system.load()
            self._loaded = True

    # -- forwarded methods (mirrors MemorySystem public API) ----------------

    # Ingestion
    def ingest(self, content: str, source: str = "inline",
               category: str = "general", memory_type: str = "episodic",
               type_config=None) -> int:
        self._ensure_loaded()
        return self._system.ingest(content, source, category, memory_type, type_config)

    def ingest_file(self, file_path: str, category: str = "tactical") -> int:
        self._ensure_loaded()
        return self._system.ingest_file(file_path, category)

    def ingest_directory(self, dir_path: str, category: str = "tactical",
                         pattern: str = "*.md") -> int:
        self._ensure_loaded()
        return self._system.ingest_directory(dir_path, category, pattern)

    def ingest_mistake(self, what_happened, correction, root_cause=None,
                       severity="medium", tags=None, source="mistake"):
        self._ensure_loaded()
        return self._system.ingest_mistake(what_happened, correction,
                                           root_cause, severity, tags, source)

    def ingest_fact(self, content, source="fact", tags=None, category="general"):
        self._ensure_loaded()
        return self._system.ingest_fact(content, source, tags, category)

    def ingest_preference(self, content, source="preference", tags=None, category="general"):
        self._ensure_loaded()
        return self._system.ingest_preference(content, source, tags, category)

    def ingest_procedure(self, content, source="procedure", tags=None, category="general"):
        self._ensure_loaded()
        return self._system.ingest_procedure(content, source, tags, category)

    # Search
    def search(self, query: str, limit: int = 20, category: str = None,
               memory_type: str = None, explain: bool = False, **kwargs) -> list:
        self._ensure_loaded()
        return self._system.search(
            query=query, limit=limit, category=category,
            memory_type=memory_type, explain=explain, **kwargs
        )

    # Persistence
    def save(self) -> str:
        return self._system.save()

    def load(self) -> int:
        """Explicitly load (also marks as loaded for lazy-load guard)."""
        self._loaded = True
        return self._system.load()

    # Context packets
    def build_context_packet(self, task: str, include_mistakes: bool = True, **kwargs):
        return self._system.build_context_packet(
            task=task, include_mistakes=include_mistakes, **kwargs
        )

    # Stats
    def stats(self) -> Dict:
        return self._system.stats()

    def get_stats(self) -> Dict:
        return self._system.get_stats()

    # Memory access
    @property
    def memories(self):
        return self._system.memories

    @property
    def decay(self):
        return self._system.decay

    # Misc
    def forget(self, **kwargs):
        return self._system.forget(**kwargs)

    def consolidate(self):
        return self._system.consolidate()

    def compact(self):
        return self._system.compact()

    def synthesize(self, research_results=None):
        return self._system.synthesize(research_results)

    def research_suggestions(self, limit=5):
        return self._system.research_suggestions(limit)

    def on_date(self, date: str):
        return self._system.on_date(date)

    def between(self, start: str, end: str):
        return self._system.between(start, end)

    def narrative(self, topic: str = None):
        return self._system.narrative(topic)

    def __repr__(self):
        return (
            f"<NamespacedMemory name={self.name!r} "
            f"memories={len(self._system.memories)} "
            f"workspace={self.workspace!r}>"
        )


# -- NamespaceManager (mixed into MemorySystem) ------------------------------

class NamespaceManager:
    """Mixin that adds namespace management to MemorySystem.

    Call ``_init_namespace_manager()`` from ``MemorySystem.__init__``.
    """

    def _init_namespace_manager(self) -> None:
        """Initialise namespace manager state.  Called by MemorySystem."""
        self._ns_manifest = NamespaceManifest(self.workspace)
        self._ns_cache: Dict[str, NamespacedMemory] = {}
        # Ensure "default" namespace is registered
        if not self._ns_manifest.exists("default"):
            self._ns_manifest.create("default")

    def _namespace_workspace(self, name: str) -> str:
        return os.path.join(self.workspace, "namespaces", name)

    # -- public API -----------------------------------------------------------

    def namespace(self, name: str) -> NamespacedMemory:
        """Return a NamespacedMemory proxy for *name*, creating it if needed.

        The returned object supports the full MemorySystem API but is fully
        isolated - search here never returns results from other namespaces.

        Args:
            name: Namespace identifier (alphanumeric, hyphens, underscores).

        Returns:
            A NamespacedMemory proxy instance.

        Example::

            ns = mem.namespace("project-alpha")
            ns.ingest("some context")
            results = ns.search("query")
        """
        _validate_name(name)
        if name not in self._ns_cache:
            # Auto-create if it doesn't exist yet
            if not self._ns_manifest.exists(name):
                self.create_namespace(name)
            ns_workspace = self._namespace_workspace(name)
            os.makedirs(ns_workspace, exist_ok=True)
            hl = getattr(self, "decay", None)
            half_life = hl.half_life if hl else 7.0
            tag_terms = getattr(self, "_tag_terms", None)
            self._ns_cache[name] = NamespacedMemory(
                name=name,
                workspace=ns_workspace,
                half_life=half_life,
                tag_terms=tag_terms,
            )
        return self._ns_cache[name]

    def create_namespace(self, name: str) -> Dict:
        """Explicitly create a namespace and return its info dict.

        Idempotent - calling again on an existing active namespace is a no-op.
        """
        _validate_name(name)
        ns_workspace = self._namespace_workspace(name)
        os.makedirs(ns_workspace, exist_ok=True)
        self._ns_manifest.create(name)
        return self._ns_manifest.get(name) or {}

    def archive_namespace(self, name: str) -> Dict:
        """Mark a namespace as archived.

        Archived namespaces still exist on disk and can be accessed via
        ``namespace(name)``, but they no longer appear in ``list_namespaces()``
        active results.

        Raises:
            KeyError: if the namespace does not exist.
        """
        self._ns_manifest.archive(name)
        # Invalidate cache
        self._ns_cache.pop(name, None)
        return self._ns_manifest.get(name) or {}

    def delete_namespace(self, name: str, delete_data: bool = False) -> None:
        """Remove a namespace from the manifest.

        Args:
            name: Namespace to delete.
            delete_data: If True, also remove the namespace's workspace
                directory.  Default False (safe).

        Raises:
            KeyError: if the namespace does not exist.
            ValueError: if attempting to delete the "default" namespace.
        """
        if name == "default":
            raise ValueError("Cannot delete the 'default' namespace.")
        self._ns_manifest.delete(name)
        self._ns_cache.pop(name, None)
        if delete_data:
            ns_workspace = self._namespace_workspace(name)
            if os.path.exists(ns_workspace):
                shutil.rmtree(ns_workspace)

    def list_namespaces(self, include_archived: bool = False) -> List[Dict]:
        """List all namespaces.

        Args:
            include_archived: If True, include archived namespaces
                (default False).

        Returns:
            List of dicts with keys: name, status, created_at, archived_at.
        """
        all_ns = self._ns_manifest.list_all()
        if include_archived:
            return all_ns
        return [ns for ns in all_ns if ns.get("status") == _STATUS_ACTIVE]
