"""parsica_spirit.context - the product facade for the markdown base tier.

The single user-facing surface of the base tier: a **scrub-gated, enrichment-wired** memory
over any StoragePort + AdminBackend. The engine is NEVER imported here - the base tier is
markdown/JSONL alone. An application escalates to the engine tier by INJECTING an engine
backend (dependency inversion); this package never reaches down into the engine.

Write path (mandatory order): scrub -> enrich(clean) -> persist(clean). A secret or PII token
can therefore never reach storage OR the enrichment index - the scrub is not optional and not
after-the-fact.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional

from parsica_spirit.enrich import EnricherPort, EnrichmentRecord, ReferenceEnricher, StoredRecord

from .backends.markdown import MarkdownBackend
from .continuity import Continuity
from .identity import (
    Identity, IdentityStatus, IdentityView,
    scope_identity as _scope_identity, load_identity_status as _load_identity_status,
    rescope_after_move as _rescope_after_move, quarantine_identity as _quarantine_identity,
    render_byline_directive as _render_byline_directive,
    render_identity_banner as _render_identity_banner,
)
from .scrub import scrub

logger = logging.getLogger("parsica_spirit")

# Labels are strict scalar metadata (exact-match filters). A nested container would let a secret
# buried below the top level slip past the label scrub onto disk - so containers are refused, not walked.
_ALLOWED_LABEL_VALUE = (str, int, float, bool, type(None))


@dataclass(frozen=True)
class WriteResult:
    """Outcome of a store(): the record id, how many secrets/PII the scrub gate redacted, and
    whether anything was persisted (stored is False when the payload was empty after scrub)."""

    id: str
    redactions: int
    stored: bool = True


class Context:
    """Scrub-gated, enrichment-wired memory over an injected StoragePort + AdminBackend.

    The backend is injected (dependency inversion) - pass a MarkdownBackend for the base tier
    or an engine backend for the engine tier; this class is identical either way.
    """

    def __init__(self, backend, *, enricher: Optional[EnricherPort] = None, scrub_pii: bool = True,
                 continuity: Optional[Continuity] = None, root: Optional[str] = None) -> None:
        self._backend = backend
        self._enricher = enricher if enricher is not None else ReferenceEnricher()
        # PII scrub is ALWAYS ON, end to end. The storage seam (backend.store -> scrub_write)
        # re-scrubs every write regardless, so a scrub_pii=False here never actually kept PII off
        # disk - it only made WriteResult.redactions UNDER-count what the backend then redacted
        # (GPT review D1). The parameter is accepted for backward compatibility and has no effect;
        # secrets and the four PII patterns are always redacted, and the counter reflects that.
        self._scrub_pii = True
        self._continuity = continuity
        self._root = root

    @property
    def tier(self) -> str:
        """Which backend class is wired - a debug affordance, not a stable public contract."""
        return type(self._backend).__name__

    @property
    def continuity(self) -> Optional[Continuity]:
        """The continuity surface (working set / journal / lessons), or None if not wired."""
        return self._continuity

    @property
    def identity(self) -> Optional[Identity]:
        """This workspace's identity - ONLY when it is valid AND bound to THIS workspace (status OK).
        On a copied/moved/corrupt workspace, or one whose identity carries no fingerprint (LEGACY), this
        is None (fail-closed): the base tier still works, but provenance shows NO identity and the
        SessionStart banner says so loudly. Never raises, never locks the agent out of store and query -
        it only withholds the identity until rescope_after_move binds one."""
        if not self._root:
            return None
        view = _load_identity_status(self._root)
        return view.identity if view.usable else None

    @property
    def identity_view(self) -> IdentityView:
        """The FULL identity classification (status + both fingerprints) for the human/audit path -
        including the quarantined record on a copied/corrupt workspace, which `identity` hides. Read-only."""
        if not self._root:
            return IdentityView(IdentityStatus.ABSENT, None, "", "")
        return _load_identity_status(self._root)

    def identity_banner(self) -> str:
        """The deterministic SessionStart identity line - loud on a copied/corrupt workspace, '' when
        unset. Pure function of on-disk state (idempotent, survives compaction)."""
        return _render_identity_banner(self._root, scrub_pii=self._scrub_pii) if self._root else ""

    def byline_directive(self, *, session_tag: str = "") -> str:
        """The byline: a directive the host injects so the agent opens every response with the name
        this workspace gave it - `[Name]`, or `[Name · <tag>]` with the host-supplied session tag.
        '' when the identity is unset, unnamed, or not usable (a copy never signs). Same purity
        contract as the banner."""
        return (_render_byline_directive(self._root, session_tag=session_tag,
                                         scrub_pii=self._scrub_pii) if self._root else "")

    def scope_identity(self, display_name: str, *, provenance: str = "self") -> Identity:
        """Mint this workspace's identity ONCE - refuses if one already exists (scoped-once). The
        display name + provenance pass the same scrub gate (honoring this Context's scrub_pii)."""
        if not self._root:
            raise RuntimeError("this Context has no workspace root for identity")
        return _scope_identity(self._root, display_name=display_name, provenance=provenance,
                               scrub_pii=self._scrub_pii)

    def rescope_after_move(self, display_name: str, *, provenance: str = "self") -> Identity:
        """Claim a copied/moved (or corrupt) workspace LOCALLY: quarantine the old identity aside (kept,
        never deleted) and mint a FRESH id bound to THIS workspace. The old id never carries across the
        boundary - a duplicated folder can't become the original, only start its own local identity."""
        if not self._root:
            raise RuntimeError("this Context has no workspace root for identity")
        fresh = _rescope_after_move(self._root, display_name=display_name, provenance=provenance,
                                    scrub_pii=self._scrub_pii)
        self._audit("rescope_after_move", new_id_prefix=fresh.identity_id[:12])
        return fresh

    def quarantine_identity(self) -> Optional[str]:
        """Move a copied/corrupt identity aside (never deletes) so the workspace can be freshly scoped;
        a no-op returning None when the identity is valid. Returns the sidecar path."""
        sidecar = _quarantine_identity(self._root) if self._root else None
        if sidecar:
            self._audit("quarantine_identity", sidecar=os.path.basename(sidecar))
        return sidecar

    # --- write path (scrub-gated) ---
    def _scrub_labels(self, labels: Optional[dict]):
        """Labels are an on-disk write path too - a caller can route free text (hence a pasted
        secret/PII) into a label key or value. Labels are STRICT SCALAR metadata: keys must be strings,
        values must be str/int/float/bool/None. A nested container (dict/list) is REFUSED (TypeError),
        because a scrub that only walked the top level would let a secret buried in a nested value reach
        records.jsonl raw - and exact-match label filtering has no use for structure anyway (put
        structured data in content). String keys and values are scrubbed so nothing lands raw; the same
        transform is applied to query()'s filter, so a store->query round-trip still matches after a
        redaction. Returns (clean_labels, redaction_count). Raises ValueError on a post-scrub key
        collision (only possible when both keys embed a secret - refuse, never silently drop one)."""
        if not labels:
            return labels, 0
        clean: dict = {}
        n = 0
        for k, v in labels.items():
            if not isinstance(k, str):
                raise TypeError("label keys must be strings; got %s" % type(k).__name__)
            ck, nk = scrub(k, scrub_pii=self._scrub_pii)
            n += nk
            if not isinstance(v, _ALLOWED_LABEL_VALUE):
                raise TypeError(
                    "label values must be scalar (str/int/float/bool/None); got %s - "
                    "store structured data in content, not labels" % type(v).__name__)
            if isinstance(v, float) and not math.isfinite(v):
                raise ValueError("label float values must be finite (no NaN/Inf)")
            cv = v
            if isinstance(v, str):
                cv, nv = scrub(v, scrub_pii=self._scrub_pii)
                n += nv
            if ck in clean and clean[ck] != cv:
                raise ValueError("label keys collide after scrubbing (a secret in a label key?): %r" % ck)
            clean[ck] = cv
        return clean, n

    def store(self, content: str, *, labels: Optional[dict] = None, enrich: bool = True) -> WriteResult:
        """Scrub the content AND the labels, enrich the CLEAN text, then persist the CLEAN text +
        CLEAN labels. An empty or whitespace-only payload (including content the scrub reduced to
        nothing) is NOT persisted - WriteResult.stored is False. Fully-redacted content still stores,
        with redactions > 0 as the signal. redactions counts content + label redactions."""
        clean, n = scrub(content or "", scrub_pii=self._scrub_pii)
        clean_labels, nl = self._scrub_labels(labels)
        if not clean.strip():
            return WriteResult(id="", redactions=n + nl, stored=False)
        enrichment: Optional[EnrichmentRecord] = None
        if enrich:
            try:
                enrichment = self._enricher.enrich(clean)
            except Exception:
                # Enrichment is an optional overlay, not part of the durability contract:
                # a bring-your-own enricher that raises must NOT lose the write. Match the
                # engine ingest path (non-fatal) - log and persist the CLEAN text unenriched.
                logger.warning("enricher.enrich() failed; storing unenriched", exc_info=True)
        rid = self._backend.store(clean, labels=clean_labels, enrichment=enrichment)
        return WriteResult(id=rid, redactions=n + nl, stored=True)

    # --- read path ---
    def query(self, text: str, *, limit: int = 15, labels: Optional[dict] = None) -> List[StoredRecord]:
        """Query, optionally filtered by labels. The label filter passes through the SAME scrub
        transform as store(), so filtering by a value that was redacted at write time (a PII/secret-
        shaped label) still matches its stored redaction - a store->query round-trip is symmetric.

        Query TEXT is ephemeral by contract: backends receive it for matching only and must never
        persist, log, or index it (users paste secrets into searches). MarkdownBackend honors this;
        any injected backend must too."""
        clean_labels, _ = self._scrub_labels(labels)
        return self._backend.query(text, limit=limit, labels=clean_labels)

    def get(self, record_id: str) -> Optional[StoredRecord]:
        return self._backend.get(record_id)

    def all_records(self) -> List[StoredRecord]:
        """Every live record, unranked - the full-dump escape hatch for audit / iteration."""
        return self._backend.all_records()

    # --- mutate ---
    def forget(self, record_id: str) -> bool:
        ok = self._backend.forget(record_id)
        if ok:
            self._audit("forget", record_id=record_id)
        return ok

    def relabel(self, record_id: str, *, labels: dict) -> bool:
        """Merge/overlay labels onto a record (existing keys are updated, others kept) - scrub-gated
        like store(), so a secret can't slip in via relabel."""
        clean_labels, _ = self._scrub_labels(labels)
        return self._backend.modify(record_id, labels=clean_labels)

    # --- reset ladder (workspace-local) - delegated to AdminBackend ---
    def plan_reset(self, *, scope: str, disposition: str):
        return self._backend.plan_reset(scope=scope, disposition=disposition)

    def execute_reset(self, plan_hash: str) -> int:
        n = self._backend.execute_reset(plan_hash)
        self._audit("execute_reset", plan_hash=plan_hash, affected=n)
        return n

    def recycle_bin(self) -> List[StoredRecord]:
        return self._backend.recycle_bin()

    def recover(self, record_id: str) -> bool:
        """Undelete a forgotten record from the recycle bin. Named `recover` (not `restore`) so it
        never collides with `ctx.continuity.restore`, which assembles the SessionStart re-inject -
        a different surface, a different verb."""
        ok = self._backend.restore(record_id)
        if ok:
            self._audit("recover", record_id=record_id)
        return ok

    # --- audit trail (metadata-only; the facade owns the clock, so the backend stays deterministic) ---
    def _audit(self, op: str, **meta) -> None:
        """Append ONE metadata-only line to <root>/audit.jsonl for a destructive / identity op - NEVER
        record content or labels (an audit that stored what it destroyed would defeat the destroy). The
        timestamp is the facade's, keeping the backend clock-free and its store/query deterministic.
        Best-effort: a logging failure never breaks the operation it records."""
        if not self._root:
            return
        entry = {"ts": datetime.now(timezone.utc).isoformat(), "op": op}
        entry.update(meta)
        try:
            with open(os.path.join(self._root, "audit.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def audit_log(self) -> List[dict]:
        """The append-only, metadata-only audit trail of destructive / identity ops on this workspace."""
        if not self._root:
            return []
        path = os.path.join(self._root, "audit.jsonl")
        if not os.path.exists(path):
            return []
        out: List[dict] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        return out


def open_markdown(root: str, *, enricher: Optional[EnricherPort] = None, scrub_pii: bool = True,
                  read_only: bool = False) -> Context:
    """Open a base-tier (markdown/JSONL) Context rooted at `root`. The engine is not involved -
    the base tier is architecturally complete without it (it ships in the box; it is simply
    not wired here - Context(EngineBackend(...)) is the explicit escalation). The store
    (records.jsonl) and the continuity surface (working.md / journal/ / lessons.md) share the one
    root; `ctx.continuity` exposes the working-set / journal / lessons surface. `~` is expanded so
    a home-relative path resolves consistently across the store, continuity, and identity.

    `scrub_pii` is accepted for backward compatibility and has NO effect: PII scrub is always on
    end to end (see Context.__init__), so it is not threaded to the store or continuity surfaces.

    read_only=True opens a NON-SCAFFOLDING view for a federated/isolated read: the backend takes
    no advisory lock and refuses every mutation, and NO continuity surface is built (Continuity's
    constructor would makedirs the root - a durable write; the federated read never touches
    continuity anyway). Opening an existing store therefore leaves ZERO new bytes on disk. Requires
    the store dir to already exist (MarkdownBackend raises FileNotFoundError otherwise) - a read
    never scaffolds."""
    root = os.path.expanduser(root)   # one boundary expansion -> store/continuity/identity share the SAME resolved root
    return Context(
        MarkdownBackend(root, read_only=read_only),
        enricher=enricher,
        continuity=None if read_only else Continuity(root, scrub_pii=True),   # always-on end to end
        root=root,
    )
