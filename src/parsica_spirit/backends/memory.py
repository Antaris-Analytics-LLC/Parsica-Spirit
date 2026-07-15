"""InMemoryBackend - the fake reference StoragePort + AdminBackend (no disk, no engine)."""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional

from parsica_spirit.enrich import EnrichmentRecord, PlanError, StoredRecord

from ._common import PlanLedger, content_id, labels_match, record_weight, searchable, tokens


class InMemoryBackend:
    def __init__(self) -> None:
        self._store: Dict[str, StoredRecord] = {}
        self._bin: Dict[str, StoredRecord] = {}
        self._plans = PlanLedger()

    # --- StoragePort ---
    def store(self, content: str, *, labels: Optional[Mapping[str, str]] = None,
              enrichment: Optional[EnrichmentRecord] = None) -> str:
        self._plans.invalidate()
        rid = content_id(content)
        self._bin.pop(rid, None)  # a re-stored record leaves the bin (live/bin stay disjoint)
        existing = self._store.get(rid)
        if existing is None:
            self._store[rid] = StoredRecord(
                id=rid, content=content, labels=dict(labels or {}),
                enrichment=enrichment if (enrichment is not None and not enrichment.is_empty()) else None,
            )
        else:  # upsert: merge labels; update enrichment only if a non-empty one is given
            existing.labels = {**dict(existing.labels), **dict(labels or {})}
            if enrichment is not None and not enrichment.is_empty():
                existing.enrichment = enrichment
        return rid

    def get(self, record_id: str) -> Optional[StoredRecord]:
        return self._store.get(record_id)

    def query(self, text: str, *, limit: int = 15, labels: Optional[Mapping[str, str]] = None) -> List[StoredRecord]:
        q = tokens(text)
        if not q:
            return []
        scored = []
        for r in self._store.values():
            if not labels_match(r.labels, labels):
                continue
            overlap = len(q & tokens(searchable(r)))
            if overlap:
                # native permanence class: fixed label-keyed weight (see _common) -
                # boosted-but-beatable, and pure, so query determinism holds.
                scored.append((overlap * record_weight(r.labels), r))
        scored.sort(key=lambda pair: (-pair[0], pair[1].id))
        return [r for _, r in scored[:limit]]

    def forget(self, record_id: str) -> bool:
        self._plans.invalidate()
        r = self._store.pop(record_id, None)
        if r is None:
            return False
        self._bin[record_id] = r
        return True

    def modify(self, record_id: str, *, labels: Optional[Mapping[str, str]] = None,
               enrichment: Optional[EnrichmentRecord] = None) -> bool:
        self._plans.invalidate()
        r = self._store.get(record_id)
        if r is None:
            return False
        if labels is not None:
            r.labels = {**dict(r.labels), **dict(labels)}  # merge - never silently drop provenance
        if enrichment is not None and not enrichment.is_empty():
            r.enrichment = enrichment  # empty enrichment is a no-op, not a clobber
        return True

    def move(self, record_id: str, *, labels: Mapping[str, str]) -> bool:
        self._plans.invalidate()
        r = self._store.get(record_id)
        if r is None:
            return False
        r.labels = {**dict(r.labels), **dict(labels)}
        return True

    def all_records(self) -> List[StoredRecord]:
        return list(self._store.values())

    # --- AdminBackend (workspace-local) ---
    def _scope_ids(self, scope: str) -> List[str]:
        if scope != "all":
            raise PlanError("scope %r not supported yet (only 'all')" % (scope,))
        return list(self._store.keys())

    def plan_reset(self, *, scope: str, disposition: str):
        return self._plans.plan(scope=scope, disposition=disposition, affected_ids=self._scope_ids(scope))

    def execute_reset(self, plan_hash: str) -> int:
        scope, disposition, affected = self._plans.verify_and_consume(plan_hash)
        if affected != frozenset(self._scope_ids(scope)):  # TOCTOU: set must still match the plan's scope
            raise PlanError("store changed since the plan was made (stale plan)")
        self._plans.invalidate()  # a reset is itself a mutation - void every other pending plan
        if disposition == "wipe":
            for rid in affected:
                r = self._store.pop(rid, None)
                if r is not None:
                    self._bin[rid] = r
        elif disposition == "migrate_local":
            # workspace-LOCAL re-home placeholder: records are PRESERVED and
            # the disposition is NEVER stamped into the caller's opaque labels.
            pass
        # "save": preserve, no change
        return len(affected)

    def recycle_bin(self) -> List[StoredRecord]:
        return list(self._bin.values())

    def restore(self, record_id: str) -> bool:
        r = self._bin.pop(record_id, None)
        if r is None:
            return False
        self._plans.invalidate()  # a successful restore changes the id-set - void pending plans
        self._store[record_id] = r
        return True
