"""MarkdownBackend - the first REAL StoragePort, on JSONL files.

The SAME conformance suite the in-memory fake passes must also pass against actual
on-disk persistence - two structurally different backends, one suite.

Concurrency: every mutation runs in a cross-process critical section (an OS advisory
lock: lock -> reload from disk -> mutate -> flush), so two windows/processes opening the
same workspace cannot last-writer-win each other's records away. Reads refresh from disk
first (a short, best-effort lock; stale-tolerant on timeout) so a long-lived instance sees
other sessions' committed writes, and they return defensive copies, never internal objects.

Layout under `root`:
    records.jsonl   - one JSON record per line (the live store)
    recycle.jsonl   - soft-deleted records (recoverable)
    .parsica_md.lock - the advisory lock file (empty)
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from typing import Dict, List, Mapping, Optional

from parsica_spirit.enrich import EnrichmentRecord, PlanError, StoredRecord
from parsica_spirit.enrich.scrub import scrub_write

from ._common import PlanLedger, content_id, labels_match, record_weight, searchable, tokens


def _copy_record(r: StoredRecord) -> StoredRecord:
    """A defensive copy so a caller mutating a returned record's labels can't corrupt the in-memory
    store (labels are mutable). enrichment is a frozen EnrichmentRecord, so it is shared safely."""
    return StoredRecord(id=r.id, content=r.content, labels=dict(r.labels or {}), enrichment=r.enrichment)


def _to_dict(r: StoredRecord) -> dict:
    return {
        "id": r.id,
        "content": r.content,
        "labels": dict(r.labels),
        "enrichment": r.enrichment.to_dict() if r.enrichment is not None else None,
    }


def _from_dict(d: dict) -> StoredRecord:
    enr = d.get("enrichment")
    return StoredRecord(
        id=d["id"],
        content=d.get("content", ""),
        labels=dict(d.get("labels") or {}),
        enrichment=EnrichmentRecord.from_dict(enr) if enr else None,
    )


class MarkdownBackend:
    def __init__(self, root: str, *, read_only: bool = False) -> None:
        self._root = root
        self._read_only = read_only
        if read_only:
            # a read-only open NEVER scaffolds: no makedirs, no chmod, and (see
            # _refresh_for_read) no advisory lock file. So a federated/isolated read that
            # opens an existing store leaves ZERO new bytes on disk. A read never creates
            # the store, so the dir must already exist.
            if not os.path.isdir(root):
                raise FileNotFoundError(
                    "read-only markdown backend requires an existing store dir: %r" % (root,))
        else:
            os.makedirs(root, exist_ok=True)
            self._chmod(root, 0o700)  # store is private to the owning user (no-op on Windows)
        self._records_path = os.path.join(root, "records.jsonl")
        self._recycle_path = os.path.join(root, "recycle.jsonl")
        self._lock_path = os.path.join(root, ".parsica_md.lock")
        self._store: Dict[str, StoredRecord] = self._load(self._records_path)
        self._bin: Dict[str, StoredRecord] = self._load(self._recycle_path)
        self._plans = PlanLedger()

    def _ensure_writable(self) -> None:
        """Every durable mutation runs through here first. A read-only backend refuses to
        write, so a federated/isolated read that opened this store can never leave a byte
        behind - not even the advisory lock file a refresh would otherwise create."""
        if self._read_only:
            raise PermissionError("markdown backend was opened read-only")

    @staticmethod
    def _chmod(path: str, mode: int) -> None:
        """Best-effort restrictive permissions; no-op where unsupported (Windows NTFS ACLs
        already scope to the owning user). Never raises - hardening must not break a write."""
        try:
            os.chmod(path, mode)
        except (OSError, NotImplementedError):
            pass

    @staticmethod
    def _load(path: str) -> Dict[str, StoredRecord]:
        out: Dict[str, StoredRecord] = {}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                        r = _from_dict(d)
                    except Exception:
                        continue  # tolerant-on-read: a bad line OR off-shape record never breaks a load
                    out[r.id] = r
        return out

    @classmethod
    def _flush(cls, store: Dict[str, StoredRecord], path: str) -> None:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for r in store.values():
                f.write(json.dumps(_to_dict(r), ensure_ascii=False) + "\n")
        cls._chmod(tmp, 0o600)  # owner-only before the rename publishes it
        os.replace(tmp, path)  # atomic swap

    def _flush_store(self) -> None:
        self._flush(self._store, self._records_path)

    def _flush_bin(self) -> None:
        self._flush(self._bin, self._recycle_path)

    # --- cross-process concurrency: advisory lock + reload-before-mutate ---
    @contextlib.contextmanager
    def _write_lock(self, timeout: float = 120.0):
        """One cross-process critical section per store for every mutation, via an OS advisory
        lock (msvcrt on Windows, fcntl on POSIX). The kernel drops the lock when the holder
        process dies, so a crash can never wedge the store. Blocking, polled acquire; raises on
        the (should-never-happen) timeout rather than silently proceeding unlocked."""
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR)
        deadline = time.monotonic() + timeout
        try:
            if os.name == "nt":
                import msvcrt
                while True:
                    try:
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("markdown store lock: not acquired within %.0fs" % timeout)
                        time.sleep(0.03)
            else:
                import fcntl
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("markdown store lock: not acquired within %.0fs" % timeout)
                        time.sleep(0.03)
            yield
        finally:
            try:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)

    def _reload(self) -> None:
        """Re-read both files from disk so a mutation applies to the CURRENT store, not this
        instance's snapshot - otherwise two processes each mutating an older snapshot would
        last-writer-win each other's records away. Does NOT invalidate pending plans: a plan made
        in this same locked flow (plan_reset) must survive the reload execute_reset performs, and a
        store that actually changed underneath is caught by execute_reset's affected-set check."""
        self._store = self._load(self._records_path)
        self._bin = self._load(self._recycle_path)

    def _locked(self, fn):
        """Run fn() inside the write lock on freshly-reloaded state, retrying the whole mutation
        on a transient cross-process error (Windows os.replace access-denied while another process
        has a store file open). Mutations are content-keyed, so a retry re-applies cleanly; the
        final attempt re-raises so a genuine, persistent failure is never swallowed."""
        last: Optional[BaseException] = None
        for attempt in range(6):
            try:
                with self._write_lock():
                    self._reload()
                    return fn()
            except (PermissionError, TimeoutError) as e:
                last = e
                time.sleep(0.05 * (attempt + 1))
        assert last is not None
        raise last

    # --- StoragePort ---
    def store(self, content: str, *, labels: Optional[Mapping[str, str]] = None,
              enrichment: Optional[EnrichmentRecord] = None) -> str:
        self._ensure_writable()
        content, labels, enrichment = scrub_write(content, labels, enrichment)   # seam-level scrub gate
        def _do() -> str:
            self._plans.invalidate()
            rid = content_id(content)
            bin_evicted = self._bin.pop(rid, None) is not None
            existing = self._store.get(rid)
            if existing is None:
                self._store[rid] = StoredRecord(
                    id=rid, content=content, labels=dict(labels or {}),
                    enrichment=enrichment if (enrichment is not None and not enrichment.is_empty()) else None,
                )
            else:
                existing.labels = {**dict(existing.labels), **dict(labels or {})}
                if enrichment is not None and not enrichment.is_empty():
                    existing.enrichment = enrichment
            self._flush_store()
            if bin_evicted:
                self._flush_bin()
            return rid
        return self._locked(_do)

    def _refresh_for_read(self) -> None:
        """Reload from disk before a read so a long-lived instance sees other sessions' committed
        writes - not just its own since the last mutation (the 'works until I open two terminals'
        trap). Best-effort: a short lock timeout (or a transient reload I/O error) falls back to a
        possibly-stale read rather than hanging or crashing a user-facing query. Safe from deadlock:
        no mutation calls the public read path, so this never re-enters a held write lock.

        Under a read-only open the reload takes NO advisory lock: the lock file is itself a durable
        write (os.open with O_CREAT), which a federated/isolated zero-footprint read must never
        create. It re-reads best-effort and tolerates a transient I/O error, exactly like the
        locked path's stale-tolerance - it just skips the lock."""
        if self._read_only:
            try:
                self._reload()
            except OSError:
                pass
            return
        try:
            with self._write_lock(timeout=2.0):
                self._reload()
        except (TimeoutError, OSError):
            pass

    def get(self, record_id: str) -> Optional[StoredRecord]:
        self._refresh_for_read()
        r = self._store.get(record_id)
        return _copy_record(r) if r is not None else None

    def query(self, text: str, *, limit: int = 15, labels: Optional[Mapping[str, str]] = None) -> List[StoredRecord]:
        self._refresh_for_read()
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
        return [_copy_record(r) for _, r in scored[:limit]]

    def forget(self, record_id: str) -> bool:
        self._ensure_writable()
        def _do() -> bool:
            self._plans.invalidate()
            r = self._store.pop(record_id, None)
            if r is None:
                return False
            self._bin[record_id] = r
            self._flush_store()
            self._flush_bin()
            return True
        return self._locked(_do)

    def modify(self, record_id: str, *, labels: Optional[Mapping[str, str]] = None,
               enrichment: Optional[EnrichmentRecord] = None) -> bool:
        self._ensure_writable()
        _, labels, enrichment = scrub_write(None, labels, enrichment)   # seam-level scrub gate
        def _do() -> bool:
            self._plans.invalidate()
            r = self._store.get(record_id)
            if r is None:
                return False
            if labels is not None:
                r.labels = {**dict(r.labels), **dict(labels)}
            if enrichment is not None and not enrichment.is_empty():
                r.enrichment = enrichment  # empty enrichment is a no-op, not a clobber
            self._flush_store()
            return True
        return self._locked(_do)

    def move(self, record_id: str, *, labels: Mapping[str, str]) -> bool:
        self._ensure_writable()
        _, labels, _ = scrub_write(None, labels)   # seam-level scrub gate
        def _do() -> bool:
            self._plans.invalidate()
            r = self._store.get(record_id)
            if r is None:
                return False
            r.labels = {**dict(r.labels), **dict(labels)}
            self._flush_store()
            return True
        return self._locked(_do)

    def all_records(self) -> List[StoredRecord]:
        self._refresh_for_read()
        return [_copy_record(r) for r in self._store.values()]

    # --- AdminBackend (workspace-local) ---
    def _scope_ids(self, scope: str) -> List[str]:
        if scope != "all":
            raise PlanError("scope %r not supported yet (only 'all')" % (scope,))
        return list(self._store.keys())

    def plan_reset(self, *, scope: str, disposition: str):
        self._ensure_writable()
        # reload under the lock so the plan's affected-id set reflects current disk truth;
        # execute_reset re-checks it, so a store that changes in between is caught as stale.
        def _do():
            return self._plans.plan(scope=scope, disposition=disposition, affected_ids=self._scope_ids(scope))
        return self._locked(_do)

    def execute_reset(self, plan_hash: str) -> int:
        self._ensure_writable()
        def _do() -> int:
            scope, disposition, affected = self._plans.verify_and_consume(plan_hash)
            if affected != frozenset(self._scope_ids(scope)):
                raise PlanError("store changed since the plan was made (stale plan)")
            self._plans.invalidate()  # a reset is itself a mutation - void every other pending plan
            if disposition == "wipe":
                for rid in affected:
                    r = self._store.pop(rid, None)
                    if r is not None:
                        self._bin[rid] = r
                self._flush_store()
                self._flush_bin()
            elif disposition == "migrate_local":
                # workspace-LOCAL re-home placeholder: records are PRESERVED and
                # the disposition is NEVER stamped into the caller's opaque labels.
                pass
            return len(affected)
        return self._locked(_do)

    def recycle_bin(self) -> List[StoredRecord]:
        self._refresh_for_read()
        return [_copy_record(r) for r in self._bin.values()]

    def restore(self, record_id: str) -> bool:
        self._ensure_writable()
        def _do() -> bool:
            r = self._bin.pop(record_id, None)
            if r is None:
                return False
            self._plans.invalidate()  # a successful restore changes the id-set - void pending plans
            self._store[record_id] = r
            self._flush_store()
            self._flush_bin()
            return True
        return self._locked(_do)
