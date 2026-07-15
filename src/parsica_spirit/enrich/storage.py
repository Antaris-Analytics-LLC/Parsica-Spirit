"""parsica_spirit.enrich.storage - the storage seam contracts (StoragePort + AdminBackend).

These live in the LEAF, not the facade, on purpose. Both the facade and the engine
implement/consume this contract by importing ONLY the leaf - so neither imports the
other and the graph stays facade -> enrich <- engine with no back-edge.

(An earlier design placed these in the adapter under a "consumer owns the Protocols"
rule. That is exactly what forces the engine to import the adapter to implement the
Protocol - the cycle the seam exists to prevent. Leaf-ownership is what decouples both
sides. The leaf is the shared *contract* layer, not only enrichment.)

OPACITY CONTRACT (non-negotiable):
  * No Claude Code types cross this boundary. `session_id` and every label are OPAQUE
    strings; the backend never interprets them (label matching is string-equality only).
  * The backend deals in opaque `StoredRecord`s. `EnrichmentRecord` is the one shared,
    intentional vocabulary (it lives in this same leaf) - backends index it; they do not
    invent their own enrichment shape.
  * VETO BY ABSENCE: identity is workspace-scoped, singular, NON-exportable. No method
    here emits a portable identity or a cross-workspace bundle; the veto is enforced by
    the capability not existing. `migrate_local` is workspace-LOCAL by definition.

ENGINE-IMPLEMENTABILITY (for the engine backend):
  * StoredRecord {id, content, labels, enrichment} is the COMPLETE wire surface. Engine-native
    scalars (importance, decay, access_count, confidence, sentiment, ...) are engine-INTERNAL,
    keyed by the record id; they never cross this seam.
  * query() MUST be side-effect-free (it is invoked twice in the determinism check).
  * The record id is opaque and backend-chosen; callers must not recompute or compare it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Mapping, Optional, Protocol, runtime_checkable

from .record import EnrichmentRecord

# Closed set of reset dispositions. A backend may not invent new ones; `migrate_local`
# is workspace-local and never a cross-workspace export.
DISPOSITIONS = ("wipe", "save", "migrate_local")


class PlanError(Exception):
    """Raised when execute_reset gets an unknown, consumed, or stale plan_hash.

    A destructive op MUST fail loudly rather than proceed on an unverified plan.
    """


@dataclass
class StoredRecord:
    """An opaque stored memory. `labels` are opaque strings (category, session_id, source...).

    Provenance/timestamps ride in `labels` (set by the caller) - no implicit clock enters
    a backend, so storage stays deterministic.
    """

    id: str
    content: str
    labels: Mapping[str, str] = field(default_factory=dict)
    enrichment: Optional[EnrichmentRecord] = None


@dataclass(frozen=True)
class ResetPlan:
    """A previewed, hashable plan for a destructive op. plan -> confirm -> execute."""

    plan_hash: str
    scope: str
    disposition: str
    affected: int


@runtime_checkable
class StoragePort(Protocol):
    """Store / retrieve / mutate opaque memory records. Backend-agnostic.

    Identity model: `store` is an UPSERT keyed by a content-addressed id. Storing the
    same content twice yields the same id and ONE record (the same fact is one memory),
    merging labels and updating enrichment if given. Content is immutable once stored; to
    change content, store new and forget old. `modify` therefore changes labels/enrichment
    only, and merges labels (it never silently drops opaque provenance).
    """

    def store(self, content: str, *, labels: Optional[Mapping[str, str]] = None,
              enrichment: Optional[EnrichmentRecord] = None) -> str: ...

    def get(self, record_id: str) -> Optional[StoredRecord]: ...

    def query(self, text: str, *, limit: int = 15,
              labels: Optional[Mapping[str, str]] = None) -> List[StoredRecord]:
        """Retrieve by text; optionally AND-filter on opaque label equality."""

    def forget(self, record_id: str) -> bool:
        """Soft-delete into the recycle bin; recoverable until purged."""

    def modify(self, record_id: str, *, labels: Optional[Mapping[str, str]] = None,
               enrichment: Optional[EnrichmentRecord] = None) -> bool:
        """Merge new labels and/or replace enrichment. Does NOT change content (immutable)."""

    def move(self, record_id: str, *, labels: Mapping[str, str]) -> bool:
        """Merge new opaque labels onto a record (e.g. re-shard)."""

    def all_records(self) -> List[StoredRecord]: ...


@runtime_checkable
class AdminBackend(Protocol):
    """Workspace-local administration: the reset-ladder surface.

    plan -> confirm -> execute is ENFORCED: `execute_reset` MUST reject (raise PlanError) any
    plan_hash not produced by a prior `plan_reset`, OR invalidated since. ANY intervening
    mutation invalidates a pending plan - store, forget, modify, move, restore, AND the
    execution of any other plan - so a destructive op never runs against state that changed
    after it was previewed. Every disposition is workspace-LOCAL; no cross-workspace movement.
    """

    def plan_reset(self, *, scope: str, disposition: str) -> ResetPlan: ...

    def execute_reset(self, plan_hash: str) -> int:
        """Execute a previously-planned, still-valid reset; return the count acted on.
        Raises PlanError on an unknown / consumed / stale plan_hash."""

    def recycle_bin(self) -> List[StoredRecord]: ...

    def restore(self, record_id: str) -> bool: ...
