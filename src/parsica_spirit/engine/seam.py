"""parsica_spirit.engine.seam - the EngineBackend behind the Parsica Spirit storage seam.

Implements the leaf's StoragePort + AdminBackend over MemorySystem, importing ONLY the
leaf (parsica_spirit.enrich) for the contracts + kit - never the adapter. The installer injects an
instance of this into the Claude Code adapter (dependency inversion) to escalate the
markdown tier to the engine tier.

Bridges between the seam (id-based, content-agnostic) and the engine (entry.hash, topic-based
forget, input-gated ingest):
  1. record id = the engine's entry.hash. entry.hash is computed once at construction from
     source:line:content and never recomputed, so for seam stores (constant source/line) the id
     is content-derived and stable. The caller's opaque labels and the exact EnrichmentRecord
     live in side maps keyed by id (enrichment is ALSO projected onto the engine's indexed
     fields so doc2query stays queryable; the projection mirrors _enr exactly - replace, not
     merge). A "source_session" label is likewise projected onto MemoryEntry.source_channel so
     engine-tier search_by_recency sees it; the label itself stays in the caller-owned side map.
     Seam-internal disposition flags live in a SEPARATE private map that never surfaces
     through StoredRecord - _labels stays strictly caller-owned.
  2. id-based forget/wipe edit mem.memories then call the engine's own _rebuild_content_indexes()
     (+ mark_dirty + read-cache invalidate) - the resync its forget() uses. The recycle bin
     carries the FULL record state (entry + labels + enrichment) so a forgotten id can be cleaned
     from the live side maps without losing what restore() needs.
  3. store() constructs a MemoryEntry and appends directly, bypassing the engine's input-quality
     gate - the seam persists unconditionally. A re-store of *forgotten* content starts CLEAN
     (no tombstone resurrection); a re-store of *live* content is an upsert (last-writer-wins).
  4. DURABILITY: the engine persists its entries via its own save/load; the seam persists its
     side maps (labels, exact enrichments, recycle bin) in a sidecar (.parsica_seam.json) with
     write-through on every mutation and load on init, so a reopened backend sees everything.
     Mutations run in a cross-process critical section (an OS advisory lock: lock -> reload ->
     mutate -> persist, with a whole-mutation retry on transient Windows write races), so two
     windows/processes on one workspace cannot last-writer-win each other's records away. Reads
     stay lock-free on the instance snapshot.
  5. query() is a pure read: the engine bumps access-counts as a side effect, so query snapshots
     and restores access state around the search, giving deterministic, repeatable results.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
import uuid
from typing import Dict, List, Mapping, Optional, Tuple

from parsica_spirit.enrich import EnrichmentRecord, PlanError, PlanLedger, StoredRecord, labels_match, tokens
from parsica_spirit.enrich.scrub import scrub_write

from .core import MemorySystem
from .entry import MemoryEntry
from .locking import LockTimeout

logger = logging.getLogger("parsica_spirit.engine.seam")


def _chmod_600(path: str) -> None:
    """Best-effort 0600 on a store file (POSIX). No-op where chmod is unsupported."""
    try:
        os.chmod(path, 0o600)
    except (OSError, NotImplementedError):
        pass


class SeamSidecarError(RuntimeError):
    """An EXISTING seam sidecar is unreadable or malformed. The seam fails CLOSED: the
    sidecar holds exactly what the engine cannot reconstruct (caller labels - including
    scope isolation - exact enrichments, the recycle bin), and one mutation performed
    over silently-emptied side maps PERSISTS the loss. A missing sidecar still means a
    fresh workspace; a broken one means repair before mutating."""


class EngineBackend:
    #: seam-owned sidecar beside the engine store: labels + exact enrichments + recycle bin,
    #: keyed by record id. The engine persists entries; the seam persists what the seam owns.
    SIDECAR = ".parsica_seam.json"

    def __init__(self, workspace: str, *, agent_name: str = "parsica") -> None:
        self._mem = self._open_engine(workspace, agent_name)
        self._labels: Dict[str, dict] = {}            # id -> caller's opaque labels (caller-owned ONLY)
        self._enr: Dict[str, EnrichmentRecord] = {}   # id -> exact seam enrichment
        # recycle bin carries FULL record state: id -> (entry, labels_snapshot, enrichment_snapshot)
        self._bin: Dict[str, Tuple[MemoryEntry, dict, Optional[EnrichmentRecord]]] = {}
        self._plans = PlanLedger()
        self._load_sidecar()

    @staticmethod
    def _open_engine(workspace, agent_name):
        """Construct + load the engine, retrying on a TRANSIENT Windows PermissionError: engine
        construction reads several store files (namespace_manifest, indexes) with no lock, and a
        concurrent process replacing one of them makes the read momentarily fail. load() itself is
        non-fatal (a fresh workspace loads nothing)."""
        last = None
        for attempt in range(8):
            try:
                mem = MemorySystem(workspace=workspace, agent_name=agent_name)
                try:
                    mem.load()                        # fresh workspace loads 0; an existing one loads all
                except Exception:
                    logger.warning("engine load failed; starting from the in-memory state", exc_info=True)
                return mem
            except PermissionError as e:
                last = e
                time.sleep(0.05 * (attempt + 1))
        raise last

    # --- durability ---
    def _sidecar_path(self) -> str:
        return os.path.join(str(self._mem.workspace), self.SIDECAR)

    def _load_sidecar(self) -> None:
        path = self._sidecar_path()
        # A transient read failure (a concurrent writer's in-flight os.replace momentarily
        # denies the open on Windows) must be RETRIED, not mistaken for corruption - the same
        # pattern _open_engine uses. __init__ calls this before any lock is held, so the retry
        # lives here. Only a genuine corruption (bad JSON / non-object) fails closed.
        d = None
        last_os_err = None
        for attempt in range(8):
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
                break
            except FileNotFoundError:
                return                                # fresh workspace: no sidecar yet
            except (PermissionError, OSError) as e:   # transient concurrent-writer race
                last_os_err = e
                time.sleep(0.05 * (attempt + 1))
            except Exception as exc:
                # genuine corruption. Fail CLOSED (never fail open to empty side maps): a
                # mutation after a silent empty-load would re-persist the store WITHOUT its
                # labels/enrichments/bin, turning corruption into durable metadata loss.
                raise SeamSidecarError(
                    "seam sidecar exists but is unreadable: %s; refusing to open the store "
                    "(mutating would persist the loss of labels/enrichments/recycle-bin). "
                    "Repair or restore it; the previous good copy is kept at %s.bak when "
                    "available." % (path, path)) from exc
        if d is None:                                 # exhausted retries on a persistent OS error
            raise SeamSidecarError(
                "seam sidecar could not be read after retries: %s; refusing to open the store. "
                "Repair or restore it from %s.bak." % (path, path)) from last_os_err
        if not isinstance(d, dict):
            raise SeamSidecarError(
                "seam sidecar is malformed (not an object): %s; refusing to open the "
                "store; repair or restore from %s.bak." % (path, path))
        self._labels = {rid: dict(v) for rid, v in (d.get("labels") or {}).items() if isinstance(v, dict)}
        self._enr = {rid: EnrichmentRecord.from_dict(v) for rid, v in (d.get("enr") or {}).items()
                     if isinstance(v, dict)}
        for rid, b in (d.get("bin") or {}).items():
            try:
                self._bin[rid] = (MemoryEntry.from_dict(b["entry"]), dict(b.get("labels") or {}),
                                  EnrichmentRecord.from_dict(b["enr"]) if b.get("enr") else None)
            except Exception:
                logger.warning("seam sidecar bin entry %r unreadable; skipped", rid, exc_info=True)

    def _persist(self) -> None:
        """Write-through after EVERY mutation. ORDER MATTERS: the seam sidecar is made durable FIRST
        (atomic temp + os.replace), THEN the engine saves its entries. On the destructive paths
        (forget / wipe) this is the difference between recoverable and lost: if the sidecar write
        fails, the engine save has not run, so the record is untouched on disk and the caller's retry
        re-applies cleanly. Engine-first would commit the removal before the recycle-bin snapshot was
        durable, and a sidecar failure would lose the record entirely. Failures RAISE: a store that
        silently stops persisting is the worst failure mode it has."""
        data = {
            "labels": self._labels,
            "enr": {rid: e.to_dict() for rid, e in self._enr.items()},
            "bin": {rid: {"entry": entry.to_dict(), "labels": labels,
                          "enr": (enr.to_dict() if enr is not None else None)}
                    for rid, (entry, labels, enr) in self._bin.items()},
        }
        path = self._sidecar_path()
        tmp = "%s.%d.%s.tmp" % (path, os.getpid(), uuid.uuid4().hex[:8])
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        _chmod_600(tmp)
        # keep the PREVIOUS good sidecar as .bak before replacing it - the manual
        # recovery point _load_sidecar names when it fails closed. Best-effort: a .bak
        # failure must not block the persist itself (the main write is still atomic).
        if os.path.exists(path):
            btmp = "%s.bak.%d.%s.tmp" % (path, os.getpid(), uuid.uuid4().hex[:8])
            try:
                with open(path, "rb") as src, open(btmp, "wb") as dst:
                    dst.write(src.read())
                _chmod_600(btmp)
                os.replace(btmp, path + ".bak")
            except OSError:
                logger.warning("seam sidecar .bak refresh failed (persist continues)", exc_info=True)
                try:
                    os.unlink(btmp)
                except OSError:
                    pass
        # Windows os.replace fails (not blocks) if a reader has the destination open; transient
        # under concurrent init/reload, so retry with backoff (POSIX succeeds first try).
        for _attempt in range(20):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if _attempt == 19:
                    raise
                time.sleep(0.02 * (_attempt + 1))
        self._mem.save()   # engine entries persisted AFTER the sidecar, so a sidecar failure is recoverable

    @contextlib.contextmanager
    def _write_lock(self, timeout=120.0):
        """One cross-process critical section per workspace for every mutation, via an OS ADVISORY
        lock (msvcrt on Windows, fcntl on POSIX) on a sidecar file. The kernel releases the lock
        automatically when the holder process dies, so there is NO stale-lock window: a crashed
        holder can never wedge the store (unlike a mkdir lock, which persists until an age threshold
        and needs unreliable liveness probing to break). Blocking, polled acquire; raises on the
        (should-never-happen) timeout rather than silently proceeding unlocked."""
        lockpath = os.path.join(str(self._mem.workspace), ".parsica_seam.lock")
        os.makedirs(os.path.dirname(lockpath) or ".", exist_ok=True)
        fd = os.open(lockpath, os.O_CREAT | os.O_RDWR)
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
                            raise TimeoutError("seam write lock: could not acquire within %.0fs" % timeout)
                        time.sleep(0.03)
            else:
                import fcntl
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("seam write lock: could not acquire within %.0fs" % timeout)
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

    def _reload(self):
        """Re-read engine + sidecar state from disk so a mutation applies to the CURRENT store,
        not this instance's snapshot: two processes each mutating an older snapshot would
        otherwise last-writer-win each other's records away. Drops in-process caches FIRST so
        load() reads disk truth (save_shard writes the file but leaves the shard cache stale, so a
        plain load() after a save on the same instance returns the pre-save cached copy)."""
        m = self._mem
        try:
            sm = getattr(m, "shard_manager", None)
            if sm is not None:
                # Rebuild the shard manager from disk: fresh INDEX (ShardIndex re-reads
                # memory_index.json in its __init__) AND empty cache. The index is otherwise read
                # once at construction and never again, so a process that started against an empty
                # store keeps an empty in-memory index and load() would iterate zero shards -- missing
                # every record other processes wrote after this instance was built.
                from .sharding import ShardManager
                m.shard_manager = ShardManager(sm.workspace)
            rc = getattr(m, "_read_cache", None)
            if rc is not None:
                rc.invalidate()
            m.load()
        except Exception:
            logger.warning("engine reload failed; mutating the in-memory snapshot", exc_info=True)
        self._labels.clear()
        self._enr.clear()
        self._bin.clear()
        self._load_sidecar()

    def _refresh_for_read(self) -> None:
        """Best-effort reload before a read so a long-lived engine-backed context sees other
        sessions' committed writes (multiple live contexts can share one workspace). Short lock;
        on contention or a transient error, fall back to the current snapshot rather than hang a
        read. Sidecar corruption still fails closed via _load_sidecar()."""
        try:
            with self._write_lock(timeout=2.0):
                self._reload()
        except (PermissionError, TimeoutError, LockTimeout, OSError):
            logger.debug("engine seam read refresh failed; using current snapshot", exc_info=True)

    def _locked(self, fn):
        """Run fn() inside the write lock on freshly-reloaded state, retrying the whole mutation on
        a TRANSIENT cross-process error: Windows os.replace access-denied while another process has a
        store file open, or a sub-lock that momentarily timed out. Mutations are content-keyed
        upserts applied to reloaded state, so a retry re-applies cleanly; the final attempt re-raises
        so a genuine, persistent failure is never swallowed."""
        last = None
        for attempt in range(6):
            try:
                with self._write_lock():
                    self._reload()
                    return fn()
            except (PermissionError, TimeoutError, LockTimeout) as e:
                last = e
                time.sleep(0.05 * (attempt + 1))
        raise last

    # --- helpers ---
    def _find(self, rid):
        for e in self._mem.memories:
            if getattr(e, "hash", None) == rid:
                return e
        return None

    def _entry_for_content(self, content):
        for e in self._mem.memories:
            if e.content == content:
                return e
        return None

    def _project_enrichment(self, entry, enrichment):
        """Mirror the enrichment onto the engine's indexed fields - FULL replace of the projected
        fields, so the index never drifts from self._enr[id]. Called only with a non-empty record
        (via _set_enrichment); an empty/None enrichment is a no-op there and never reaches here."""
        if enrichment is None:
            enrichment = EnrichmentRecord()
        entry.enriched_summary = enrichment.summary or ""
        entry.search_queries = list(enrichment.search_queries) if enrichment.search_queries else []
        try:
            entry.search_keywords = list(enrichment.keywords) if enrichment.keywords else []
        except AttributeError:
            logger.warning("engine entry lacks search_keywords; skipping keyword projection")
        # the seam only ever sets tags from enrichment, so replace rather than union
        entry.tags = list(enrichment.tags) if enrichment.tags else []

    @staticmethod
    def _project_session(entry, labels):
        """Mirror a "source_session" label onto the engine's recency field
        (MemoryEntry.source_channel) so engine-tier search_by_recency's exclude_channel sees
        which session a record came from - the same projection pattern as _project_enrichment:
        the label stays caller-owned in the side map (adapter-tier query filters on labels);
        the entry field is the engine's view. Called after EVERY label write with the labels
        AS STORED (post-scrub, post-merge), so the field can never diverge from the label.
        Absent/empty label = strict no-op: never clears an existing channel, and a record
        without the label behaves exactly as before."""
        sess = (labels or {}).get("source_session")
        if sess:
            entry.source_channel = str(sess)

    def _dirty(self):
        # Replicate MemorySystem.forget()'s post-mutation resync after a raw memories edit.
        # Failures are LOGGED, never swallowed silently - a stale index after a destructive edit
        # is the most dangerous failure mode here.
        steps = (
            ("rebuild_content_indexes", lambda: self._mem._rebuild_content_indexes()),
            ("search_mark_dirty", lambda: self._mem.search_engine.mark_dirty()),
            ("read_cache_invalidate",
             lambda: self._mem._read_cache.invalidate() if getattr(self._mem, "_read_cache", None) else None),
        )
        for name, fn in steps:
            try:
                fn()
            except Exception:
                logger.warning("seam resync step %r failed", name, exc_info=True)

    def _rec(self, e):
        rid = e.hash
        return StoredRecord(id=rid, content=e.content, labels=dict(self._labels.get(rid, {})),
                            enrichment=self._enr.get(rid))

    def _set_enrichment(self, rid, entry, enrichment):
        """Set the exact enrichment and mirror it onto the engine's indexed fields. Returns True if
        anything changed (the caller is responsible for _dirty()). An empty/None enrichment is a
        NO-OP and never clobbers an existing one - clear-to-empty is intentionally not offered, and
        this is conformance-locked (empty re-store must not wipe a populated enrichment)."""
        if enrichment is None or enrichment.is_empty():
            return False
        self._enr[rid] = enrichment
        self._project_enrichment(entry, enrichment)
        return True

    # --- StoragePort ---
    def store(self, content, *, labels=None, enrichment=None):
        content, labels, enrichment = scrub_write(content, labels, enrichment)   # seam-level scrub gate
        return self._locked(lambda: self._store_inner(content, labels=labels, enrichment=enrichment))

    def _store_inner(self, content, *, labels=None, enrichment=None):
        self._plans.invalidate()
        entry = self._entry_for_content(content)
        if entry is None:
            # Fresh (or RE-stored-after-forget) content. id is content-derived, so a forgotten
            # tombstone hashes to the same id - start CLEAN, never inherit its metadata.
            entry = MemoryEntry(content=content, source="seam", category="general")
            assert entry.hash, "MemoryEntry.hash must be populated at construction"
            rid = entry.hash
            self._mem.memories.append(entry)
            self._bin.pop(rid, None)
            self._labels[rid] = dict(labels or {})    # reset, not merge
            self._project_session(entry, self._labels[rid])
            self._enr.pop(rid, None)
            self._set_enrichment(rid, entry, enrichment)
            self._dirty()
            self._persist()
            return rid
        # Existing LIVE content - upsert (last-writer-wins on label keys).
        rid = entry.hash
        self._labels[rid] = {**self._labels.get(rid, {}), **dict(labels or {})}
        self._project_session(entry, self._labels[rid])
        if self._set_enrichment(rid, entry, enrichment):
            self._dirty()
        self._persist()
        return rid

    def get(self, record_id):
        self._refresh_for_read()
        e = self._find(record_id)
        return self._rec(e) if e is not None else None

    def query(self, text, *, limit=15, labels=None):
        self._refresh_for_read()
        if not tokens(text):
            return []
        # query() is a PURE read: snapshot the access bookkeeping the engine mutates on search
        # and restore it, so identical inputs yield identical results regardless of access churn.
        import copy as _copy
        # snapshot EVERY field search() mutates as a side effect: per-entry access_count,
        # last_accessed AND importance (reinforce() bumps it and importance feeds ranking),
        # plus the AccessTracker counts - otherwise identical inputs can reorder after churn.
        snap = [(e, e.access_count, e.last_accessed, e.importance) for e in self._mem.memories]
        _at = getattr(self._mem, "_access_tracker", None)
        _at_snap = _copy.deepcopy(_at.__dict__) if _at is not None else None

        def _restore():
            for e, ac, la, imp in snap:
                e.access_count = ac
                e.last_accessed = la
                e.importance = imp
            if _at is not None and _at_snap is not None:
                _at.__dict__.clear()
                _at.__dict__.update(_copy.deepcopy(_at_snap))

        try:
            total = len(self._mem.memories)
            fetch = max(limit * 3, 50)
            out: List[StoredRecord] = []
            while True:
                _restore()  # each search sees the same pristine baseline -> deterministic ranking
                results = self._mem.search(text, limit=fetch)
                out = []
                for r in results:
                    e = getattr(r, "entry", r)
                    rid = getattr(e, "hash", None)
                    if rid is None:
                        continue
                    if not labels_match(self._labels.get(rid, {}), labels):
                        continue
                    out.append(self._rec(e))
                    if len(out) >= limit:
                        break
                # grow the window only if a selective label starved the result below limit
                if len(out) >= limit or not results or fetch >= total:
                    break
                fetch *= 2
        finally:
            _restore()
        return out

    def forget(self, record_id):
        return self._locked(lambda: self._forget_inner(record_id))

    def _forget_inner(self, record_id):
        self._plans.invalidate()
        e = self._find(record_id)
        if e is None:
            return False
        # bin the FULL state, then clean the live side maps (no leak, no resurrection)
        self._bin[record_id] = (e, dict(self._labels.get(record_id, {})), self._enr.get(record_id))
        self._mem.memories = [m for m in self._mem.memories if m.hash != record_id]
        self._labels.pop(record_id, None)
        self._enr.pop(record_id, None)
        self._dirty()
        self._persist()
        return True

    def modify(self, record_id, *, labels=None, enrichment=None):
        _, labels, enrichment = scrub_write(None, labels, enrichment)   # seam-level scrub gate
        return self._locked(lambda: self._modify_inner(record_id, labels=labels, enrichment=enrichment))

    def _modify_inner(self, record_id, *, labels=None, enrichment=None):
        self._plans.invalidate()
        e = self._find(record_id)
        if e is None:
            return False
        if labels is not None:
            self._labels[record_id] = {**self._labels.get(record_id, {}), **dict(labels)}
            self._project_session(e, self._labels[record_id])
        if self._set_enrichment(record_id, e, enrichment):
            self._dirty()
        self._persist()
        return True

    def move(self, record_id, *, labels):
        _, labels, _ = scrub_write(None, labels)   # seam-level scrub gate
        return self._locked(lambda: self._move_inner(record_id, labels=labels))

    def _move_inner(self, record_id, *, labels):
        self._plans.invalidate()
        e = self._find(record_id)
        if e is None:
            return False
        self._labels[record_id] = {**self._labels.get(record_id, {}), **dict(labels)}
        self._project_session(e, self._labels[record_id])
        self._persist()
        return True

    def all_records(self):
        self._refresh_for_read()
        return [self._rec(e) for e in self._mem.memories]

    # --- AdminBackend (workspace-local) ---
    def _scope_ids(self, scope):
        if scope != "all":
            raise PlanError("scope %r not supported yet (only 'all')" % (scope,))
        return [e.hash for e in self._mem.memories]

    def plan_reset(self, *, scope, disposition):
        self._refresh_for_read()
        return self._plans.plan(scope=scope, disposition=disposition, affected_ids=self._scope_ids(scope))

    def execute_reset(self, plan_hash):
        return self._locked(lambda: self._execute_reset_inner(plan_hash))

    def _execute_reset_inner(self, plan_hash):
        scope, disposition, affected = self._plans.verify_and_consume(plan_hash)
        if affected != frozenset(self._scope_ids(scope)):
            raise PlanError("store changed since the plan was made (stale plan)")
        self._plans.invalidate()
        if disposition == "wipe":
            keep = []
            for e in self._mem.memories:
                if e.hash in affected:
                    self._bin[e.hash] = (e, dict(self._labels.get(e.hash, {})), self._enr.get(e.hash))
                    self._labels.pop(e.hash, None)
                    self._enr.pop(e.hash, None)
                else:
                    keep.append(e)
            self._mem.memories = keep
            self._dirty()
            self._persist()
        elif disposition == "migrate_local":
            # workspace-LOCAL re-home placeholder: records are PRESERVED and
            # the disposition is NEVER stamped into the caller's opaque labels.
            pass
        return len(affected)

    def recycle_bin(self):
        self._refresh_for_read()
        # built from the bin SNAPSHOT (live side maps were cleared on forget/wipe)
        return [StoredRecord(id=rid, content=entry.content, labels=dict(labels), enrichment=enr)
                for rid, (entry, labels, enr) in self._bin.items()]

    def restore(self, record_id):
        return self._locked(lambda: self._restore_inner(record_id))

    def _restore_inner(self, record_id):
        if record_id not in self._bin:
            return False
        # if the content was re-stored after forget, the id is already live - don't duplicate it
        if self._find(record_id) is not None:
            self._bin.pop(record_id, None)
            return False
        entry, labels, enr = self._bin.pop(record_id)
        self._plans.invalidate()
        self._mem.memories.append(entry)
        self._labels[record_id] = dict(labels)
        self._project_session(entry, self._labels[record_id])   # defensive: field mirrors the label,
        #                                                          independent of the binned entry's state
        if enr is not None:
            self._enr[record_id] = enr
            self._project_enrichment(entry, enr)   # defensive: guarantee index fields mirror _enr,
            #                                         independent of how the binned entry was built
        self._dirty()
        self._persist()
        return True
