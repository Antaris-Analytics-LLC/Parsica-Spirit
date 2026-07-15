"""parsica_spirit.promote - Weave -> Spirit promotion. A planner first, a writer second.

Promotion moves warm Weave memory (journal entries, lessons, pinned working facts,
decision-bearing enriched turns) down into the durable Spirit floor, explicitly and
idempotently. It is never wired to a hook.

The load-bearing defense (the base-tier provenance invariant): the
store keys a record id on its CONTENT hash and silently merges labels on a collision,
so two promoted records with identical scrubbed bodies but different provenance would
collapse onto one id and destroy each other's provenance if provenance lived only in
labels. Every promoted record therefore leads with a deterministic, PII-free provenance
tag folded INTO the stored text (the claude_memory.ClaudeRecord.content precedent), so
records that share a body render to different content and take different ids.

Identity vocabulary, never conflated:
  * scope_id        - the ISOLATION key (which store partition this belongs to);
  * source_project  - PROVENANCE (which Weave project produced it; the Weave project
                      key, already a path-safe slug+hash);
  * source_session  - PROVENANCE (which session authored the source; sess8, '' when
                      unknowable). A LABEL only, never a pk/ledger hash input;
  * source_record_id - the stable per-kind identity of the Weave source (its
                      per-kind derivation, stable across ordinal moves);
  * pk              - the promotion idempotency slot, hex, scrub-stable;
  * content_hash    - hash of the ORIGINAL pre-promotion body (NOT the rendered text):
                      one value serves change detection AND dedup, because
                      the raw Weave copy hashes to the same value.

All writes go through Context.store / Context.forget (scrub-gated); the seam is never
called directly, and all reads go through the router gateways (enforced by AST).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from ._lock import LockHeld, exclusive_lock
from .claude_memory import split_markdown
from .router import RouterScope, _spirit_records

ROUTER_VERSION = "1"
PROMOTE_SOURCE = "parsica-weave"

#: source kinds that qualify for promotion. Instruction-class,
#: open-loops, in-progress turns, and the living working.md as a whole NEVER qualify.
#: LOAD-BEARING: PromotionCandidate refuses any other kind at construction, so a future
#: caller cannot mint an out-of-contract candidate past this constant.
SOURCE_KINDS = ("journal", "lesson", "pinned_fact", "enriched_turn")

REFUSAL_MARKER = "<redacted>"


def _h16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _apply_refusal(text: str, terms) -> str:
    """The operator's refusal fence, applied at COLLECTION time with the CURRENT list
    (Weave semantics: case-insensitive substring). The floor must honor a term added
    AFTER capture - the durable store deserves at least the mirror's protection, and
    re-fencing changes the body hash, so an already-promoted record updates in place."""
    for t in (terms or []):
        if isinstance(t, str) and t.strip():
            text = re.sub(re.escape(t.strip()), REFUSAL_MARKER, text, flags=re.IGNORECASE)
    return text


@dataclass(frozen=True)
class PromotionCandidate:
    """One Weave record proposed for the durable floor. Frozen: a candidate is a fact
    about the source at collection time, not a mutable work item."""

    scope_id: str
    source_project: str            # Weave project key (slug+hash) - provenance, not isolation
    source_kind: str               # one of SOURCE_KINDS, enforced below
    source_record_id: str          # stable per-kind id, e.g. "journal:2026-07-02:0:ab12cd34"
    body: str                      # the already-scrubbed Weave text, verbatim
    extra_labels: Dict[str, str] = field(default_factory=dict)
    source_session: str = ""       # provenance: the sess8 that authored the source ('' = unknown)
    event_ts: str = ""             # two-clocks: when the source happened (ISO; '' = unrecoverable)

    def __post_init__(self) -> None:
        if self.source_kind not in SOURCE_KINDS:
            raise ValueError("source_kind %r is not promotable (allowed: %s)"
                             % (self.source_kind, ", ".join(SOURCE_KINDS)))

    @property
    def source_project8(self) -> str:
        """Compact provenance for the content tag: hash of the project key, so the tag
        stays short and never grows a path-shaped string."""
        return hashlib.sha256(self.source_project.encode("utf-8")).hexdigest()[:8]

    @property
    def provenance_key(self) -> str:
        """Stable identity of this source across promotions (the idempotency slot).
        Hex, so it survives label scrubbing. Keyed on provenance, never on content:
        the same source with an edited body keeps its pk (that is an update, not a
        new record)."""
        raw = "%s\x00%s\x00%s" % (self.source_project, self.source_kind, self.source_record_id)
        return _h16(raw)

    @property
    def content_hash(self) -> str:
        """Hash of the ORIGINAL body - change detection and dedup both key
        on this, so the provenance prefix in the stored text never defeats a match
        against the raw Weave copy."""
        return _h16(self.body)

    @property
    def content(self) -> str:
        """The stored text: a deterministic PII-free provenance tag, then the body.
        Two candidates with identical bodies but different provenance render to
        different text, hence different content-derived record ids."""
        return "[weave %s · %s · %s · src:%s]\n\n%s" % (
            self.source_project8, self.source_kind, self.source_record_id,
            self.content_hash[:8], self.body)

    @property
    def ledger_key(self) -> str:
        """The promotion sync ledger slot for this source. Keyed on scope_id + the
        provenance_key (which folds source_project in), NOT on source_kind +
        source_record_id: two projects that share a scope_id, source_kind, and
        source_record_id must take DISTINCT ledger slots, or the second silently
        overwrites the first. provenance_key is a 16-hex digest, so the key stays short
        and path-safe."""
        return "weave:%s:%s" % (self.scope_id, self.provenance_key)

    def labels(self) -> Dict[str, str]:
        lb = dict(self.extra_labels)
        lb.update({
            "scope_id": self.scope_id,               # isolation
            "source": PROMOTE_SOURCE,
            "source_kind": self.source_kind,
            "source_project": self.source_project,   # provenance
            "source_record_id": self.source_record_id,
            "pk": self.provenance_key,
            "content_hash": self.content_hash,
            "router_version": ROUTER_VERSION,
        })
        # The two clocks (locked contract), LABELS ONLY - pk and ledger identity must
        # not move: `created` = when this record entered the pool (stamped at store
        # time; pool recency keys on it), `event_ts` = when the source actually
        # happened (chunk ts / doc stamp), emitted only when recoverable.
        if self.source_session:
            lb["source_session"] = self.source_session
        lb["created"] = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime())
        if self.event_ts:
            lb["event_ts"] = self.event_ts
        return lb


@dataclass(frozen=True)
class PromotionAction:
    """One planned step: what the applier will do and, first-class, WHY."""

    kind: str                      # store | update | skip | retire
    candidate: PromotionCandidate
    old_spirit_id: Optional[str] = None
    reason: str = ""


# ---------------------------------------------------------------------------
# Candidate collection - reads already-scrubbed store files only (working set,
# lessons.md, journal/*.md, enrich/*.jsonl; the legacy layout keeps all four under
# the project dir, the workspace layout splits them - see collect_candidates).
# NEVER the raw transcript; mirror chunks are joined for their sess/ts PROVENANCE
# fields only, no text (raw chatter does not promote; the distilled enrichment
# overlay does). Every parse is defensive: a malformed
# file or line is skipped, never a crash - promotion is bookkeeping, and
# bookkeeping must not wedge on one bad shard.
#
# Stable identity per kind. Append-only sources (lessons,
# journal) key on content hashes - stable BECAUSE append-only, and drift-proof
# against ordinal moves. Consequence, documented and deliberate: an edited
# pinned fact or a reworded lesson is a NEW durable record, not an update; the
# old copy stays as history (NEVER-assume-delete-friendly). The update path
# (same pk, changed body) fires for enriched turns, whose chunk id is stable
# while re-enrichment reshapes the overlay.
# ---------------------------------------------------------------------------

_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
#: workspace law #2 stamps, parsed off shared-doc entries: the full session form and
#: migration's retro form (the when is unknowable there, the where is not).
_STAMP_RE = re.compile(
    r"^\*\((\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}) UTC · session (\S{1,8}) · ([^)\n]+)\)\*$")
_MIGRATED_STAMP_RE = re.compile(r"^\*\(migrated · ([^)\n]+)\)\*$")


def _h8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _stamp_provenance(body: str) -> Tuple[str, str, str]:
    """(project, sess8, event_ts) from a shared-doc entry's TRAILING provenance stamp
    (workspace law #2) - only the last non-blank line qualifies, so a stamp merely
    QUOTED mid-body never wins. sess8 '-' (writer did not know its session) and
    absent fields come back as ''."""
    for ln in reversed(body.splitlines()):
        ln = ln.strip()
        if not ln:
            continue
        m = _STAMP_RE.match(ln)
        if m:
            date, hm, sess, proj = m.groups()
            return proj.strip(), ("" if sess == "-" else sess), "%sT%sZ" % (date, hm)
        m = _MIGRATED_STAMP_RE.match(ln)
        if m:
            return m.group(1).strip(), "", ""
        break
    return "", "", ""


def _sections(text: str) -> Iterator[Tuple[str, int, str]]:
    """Leaf sections of a store doc as (heading, occurrence, body). In a doc with an
    H1 (working.md / lessons.md templates) the H1 preamble is template prose, not
    memory - only H2-and-deeper sections qualify. Journal files are appended
    fragments with no H1; every section qualifies. HTML comments (the templates'
    format hints) are never content."""
    # comments are stripped from the WHOLE doc before splitting: a template comment can
    # embed an example `## heading` (lessons.md does), and split_markdown is fence-aware
    # but not comment-aware - split-then-strip would mint a phantom section from it.
    text = _COMMENT_RE.sub("", text)
    has_h1 = text.lstrip().startswith("# ")
    occ: Counter = Counter()
    for path, body in split_markdown(text):
        if has_h1 and len(path) < 2:
            continue
        heading = path[-1] if path else "(preamble)"
        body = body.strip()
        if not body:
            continue
        # occurrence is PER HEADING PATH (the claude_memory precedent), not positional:
        # inserting a new, differently-headed entry above an old one shifts nothing.
        o = occ[path]
        occ[path] += 1
        yield heading, o, body


def _bullets(body: str) -> List[str]:
    """Top-level `- ` items of a section body, wrapped continuation lines folded in.
    A blank line ends an item (facts are terse by convention)."""
    items: List[str] = []
    cur: List[str] = []
    for ln in body.splitlines():
        s = ln.strip()
        if s.startswith("- "):
            if cur:
                items.append(" ".join(cur))
            cur = [s[2:].strip()]
        elif cur and s:
            cur.append(s)
        elif cur:
            items.append(" ".join(cur))
            cur = []
    if cur:
        items.append(" ".join(cur))
    return [i for i in items if i]


def _pinned_facts(working_file: Path) -> Iterator[Tuple[str, str]]:
    """(source_record_id, body) per pinned fact. ONLY the Pinned facts section -
    open loops are live threads, ephemeral by design, never promoted."""
    try:
        text = working_file.read_text(encoding="utf-8")
    except OSError:
        return
    for heading, _occ, body in _sections(text):
        if heading.strip().lower() != "pinned facts":
            continue
        for fact in _bullets(body):
            norm = " ".join(fact.split())
            yield "working:pinned:" + _h16(norm), fact


def _doc_entries(text: str, kind: str, date: Optional[str]) -> Iterator[Tuple[str, str]]:
    """(source_record_id, body) per lesson/journal section. The body leads with the
    heading - a scar's title is content, and the provenance tag carries only hashes."""
    for heading, occurr, sec_body in _sections(text):
        body = "%s\n\n%s" % (heading.strip(), sec_body) if heading != "(preamble)" else sec_body
        d = date
        if d is None:
            m = _DATE_RE.search(heading)
            d = m.group(1) if m else "undated"
        yield "%s:%s:%d:%s" % (kind, d, occurr, _h8(body)), body


def _load_decisions_overlay(pdir: Path, include_archive: bool = False) -> Dict[str, dict]:
    """chunk id -> latest enrichment overlay, resolved deterministically on content
    ((spec, at, run_id) - mirroring enrich.load_overlay's resolution), never on file
    or append order. include_archive: the workspace layout ROTATES overlay shards to
    enrich/archive/ (never deletes) - rotated decisions still promote."""
    ed = pdir / "enrich"
    files = sorted(ed.glob("*.jsonl")) if ed.exists() else []
    if include_archive and (ed / "archive").exists():
        files += sorted((ed / "archive").glob("*.jsonl"))
    chosen: Dict[str, Tuple[Tuple[str, str, str], dict]] = {}
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                o = json.loads(ln)
            except ValueError:
                continue
            if not (isinstance(o, dict) and o.get("id") and isinstance(o.get("enrich"), dict)):
                continue
            e = o["enrich"]
            k = (str(e.get("spec") or ""), str(e.get("at") or ""), str(e.get("run_id") or ""))
            cur = chosen.get(o["id"])
            if cur is None or k >= cur[0]:
                chosen[o["id"]] = (k, e)
    return {cid: e for cid, (_k, e) in chosen.items()}


def _mirror_provenance(mirror_dir: Path) -> Dict[str, Tuple[str, str]]:
    """chunk id -> (sess, ts) joined back from the capture mirror (active shards and
    rotation's archive/). PROVENANCE ONLY: raw chatter still never promotes - this
    join reads two identity fields off each chunk and no text. Defensive like every
    parser here: a malformed shard or line is skipped, never a crash."""
    out: Dict[str, Tuple[str, str]] = {}
    for d in (mirror_dir, mirror_dir / "archive"):
        if not d.exists():
            continue
        for f in sorted(d.glob("*.jsonl")):
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for ln in lines:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    o = json.loads(ln)
                except ValueError:
                    continue
                if not (isinstance(o, dict) and o.get("id")):
                    continue
                sess = str(o.get("sess") or "").strip()
                ts = str(o.get("ts") or "").strip()
                if sess or ts:
                    out[str(o["id"])] = (sess, ts)
    return out


def _render_enriched_turn(enrich: dict) -> Optional[str]:
    """A decision-bearing overlay rendered to a deterministic durable body: summary,
    then each decision with its conviction-at-creation. Returns None when the overlay
    carries no real decision - raw chatter does not promote."""
    decisions = [d for d in (enrich.get("decisions") or [])
                 if isinstance(d, dict) and str(d.get("text") or "").strip()]
    if not decisions:
        return None
    lines: List[str] = []
    summary = enrich.get("summary")
    if isinstance(summary, str) and summary.strip():
        lines.append(summary.strip())
    lines.append("Decisions:")
    for d in decisions:
        conv = d.get("conviction") if isinstance(d.get("conviction"), dict) else {}
        val = conv.get("value")
        suffix = " (conviction %s)" % val if isinstance(val, (int, float)) else ""
        lines.append("- %s%s" % (str(d["text"]).strip(), suffix))
    return "\n".join(lines)


def collect_candidates(weave_project_dir, *, scope_id: str,
                       source_project: Optional[str] = None,
                       refusal=None,
                       source_session: Optional[str] = None,
                       workspace_root=None) -> List[PromotionCandidate]:
    """Every promotion-eligible record for a Weave project, in deterministic order
    (pinned facts, lessons, journal by date, enriched turns by chunk id). Two layouts,
    feature-detected by the caller (both new kwargs optional, so either side upgrades
    first):

      * legacy (workspace_root=None): all four surfaces live under weave_project_dir
        (working.md, lessons.md, journal/, enrich/) - today's behavior, unchanged;
      * workspace (workspace_root=the Weave STORE_ROOT): lessons.md, journal/ (active
        pages + archive/), and working/"<key> - Working Set.md" are WORKSPACE docs
        shared by every project; weave_project_dir is projects/<key>, still the home
        of enrich/ (+ archive/ once rotation has run).

    The Weave project key IS the store dir name, so source_project defaults to it.
    `refusal` is the source project's CURRENT refusal list, applied to every body on
    the way out - a term fenced after capture never reaches the durable floor.

    `source_session` (the promoting session's sess8) is the FALLBACK per-candidate
    session; a source that can testify to its own author outranks it: enriched turns
    join their chunk id back to the capture mirror (sess + ts), lesson/journal entries
    carry the workspace provenance stamp (law #2). In workspace mode an entry stamped
    by ANOTHER project still promotes but keeps that project as its provenance - so
    every promoting project renders the SAME candidate (same pk, same content) and the
    planner's pk match plus the store's content-derived id dedup a shared doc instead
    of double-storing it once per project."""
    pdir = Path(weave_project_dir)
    sp = source_project or pdir.name
    ws = Path(workspace_root) if workspace_root is not None else None
    fb_sess = (source_session or "").strip()
    out: List[PromotionCandidate] = []

    def _add(kind: str, rid: str, body: str, project: str = "",
             sess: str = "", event_ts: str = "") -> None:
        out.append(PromotionCandidate(scope_id=scope_id, source_project=project or sp,
                                      source_kind=kind, source_record_id=rid,
                                      body=_apply_refusal(body, refusal),
                                      source_session=sess or fb_sess,
                                      event_ts=event_ts))

    def _doc_add(kind: str, rid: str, body: str) -> None:
        # the trailing stamp outranks the fallback; the stamped PROJECT re-homes a
        # candidate only in workspace mode - legacy docs are per-project, and re-keying
        # them would orphan existing ledger entries (pk feeds on source_project).
        proj, sess, ets = _stamp_provenance(body)
        _add(kind, rid, body, project=(proj if ws is not None else ""),
             sess=sess, event_ts=ets)

    working = (ws / "working" / ("%s - Working Set.md" % sp)) if ws is not None \
        else (pdir / "working.md")
    for rid, body in _pinned_facts(working):
        _add("pinned_fact", rid, body)

    lessons_file = (ws / "lessons.md") if ws is not None else (pdir / "lessons.md")
    try:
        lessons = lessons_file.read_text(encoding="utf-8")
    except OSError:
        lessons = ""
    for rid, body in _doc_entries(lessons, "lesson", None):
        _doc_add("lesson", rid, body)

    if ws is not None:
        jroot = ws / "journal"
        jfiles = [f for d in (jroot, jroot / "archive") if d.exists()
                  for f in d.glob("*.md")]
        jfiles.sort(key=lambda f: (f.stem, str(f)))
    else:
        jd = pdir / "journal"
        jfiles = sorted(jd.glob("*.md")) if jd.exists() else []
    for jf in jfiles:
        try:
            text = jf.read_text(encoding="utf-8")
        except OSError:
            continue
        for rid, body in _doc_entries(text, "journal", jf.stem):
            _doc_add("journal", rid, body)

    overlay = _load_decisions_overlay(pdir, include_archive=ws is not None)
    mirror = _mirror_provenance((ws / "mirror" / sp) if ws is not None
                                else (pdir / "mirror")) if overlay else {}
    for cid in sorted(overlay):
        body = _render_enriched_turn(overlay[cid])
        if body:
            sess, ts = mirror.get(cid, ("", ""))
            _add("enriched_turn", "enriched:" + cid, body, sess=sess, event_ts=ts)

    return out


# ---------------------------------------------------------------------------
# The promotion sync ledger. A sync_id label on a FORGOTTEN record
# is invisible to the next planning pass - the recycle bin is excluded from
# all_records - so retirement intent needs its own home. The ledger is NOT a
# second source of truth for existence: the store is truth for what exists; the
# ledger is truth only for retirement intent and sync_id. Content-free BY
# CONSTRUCTION (ids, hashes, enums, ints - never free text), written atomically
# (unique temp + os.replace, the store's own precedent), by promotion alone.
# ---------------------------------------------------------------------------

LEDGER_DIRNAME = ".parsica_router"
LEDGER_FILENAME = "promotion_sync.json"


def _chmod(path, mode: int) -> None:
    """Best-effort restrictive permissions; no-op where unsupported (Windows NTFS ACLs
    already scope to the owning user). Never raises - hardening must not break a write.
    Mirrors MarkdownBackend._chmod so the ledger's atomic writer matches the store's."""
    try:
        os.chmod(path, mode)
    except (OSError, NotImplementedError):
        pass


class PromotionBlocked(RuntimeError):
    """Promotion refused before any write (missing identity, unusable workspace)."""


class SyncLedger:
    def __init__(self, spirit_root, scope_id: str) -> None:
        self.root = Path(spirit_root)
        self._path = self.root / LEDGER_DIRNAME / LEDGER_FILENAME
        self._scope_id = scope_id
        self._data = self._load()

    def _load(self) -> dict:
        try:
            d = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        if not isinstance(d, dict):
            d = {}
        d.setdefault("version", 1)
        d.setdefault("scope_id", self._scope_id)
        d.setdefault("next_sync_id", 1)
        d.setdefault("records", {})
        return d

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name("%s.%s.%s.tmp" % (
            self._path.name, os.getpid(), threading.get_ident()))
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, allow_nan=False,
                                  indent=1, sort_keys=True), encoding="utf-8")
        _chmod(tmp, 0o600)              # owner-only before the rename publishes it
        tmp.replace(self._path)
        _chmod(self._path, 0o600)      # and the published ledger stays owner-only

    def entry(self, ledger_key: str) -> Optional[dict]:
        e = self._data["records"].get(ledger_key)
        return e if isinstance(e, dict) else None

    def is_retired(self, ledger_key: str) -> bool:
        e = self.entry(ledger_key)
        return bool(e and e.get("state") == "retired")

    def _next_sync_id(self) -> int:
        n = int(self._data.get("next_sync_id", 1))
        self._data["next_sync_id"] = n + 1
        return n

    def mark_active(self, candidate: PromotionCandidate, spirit_id: str, *, action: str) -> None:
        self._data["records"][candidate.ledger_key] = {
            "pk": candidate.provenance_key,
            "current_spirit_id": spirit_id,
            "current_content_hash": candidate.content_hash,
            "sync_id": self._next_sync_id(),
            "state": "active",
            "last_action": action,
        }
        self.save()

    def mark_retired(self, ledger_key: str, *, pk: str = "") -> None:
        e = self._data["records"].get(ledger_key)
        e = e if isinstance(e, dict) else {"pk": pk}
        e.update({"state": "retired", "last_action": "retire",
                  "sync_id": self._next_sync_id()})
        e.pop("current_spirit_id", None)
        self._data["records"][ledger_key] = e
        self.save()

    def active_keys(self) -> Dict[str, dict]:
        return {k: e for k, e in self._data["records"].items()
                if isinstance(e, dict) and e.get("state") == "active"}

    def entries(self) -> Dict[str, dict]:
        """EVERY ledger entry, active AND retired, as defensive copies - the read surface
        for migration tooling (scope merges must carry retirement intent, which lives
        only here; a retired source has no live record to testify for it)."""
        return {k: dict(e) for k, e in self._data["records"].items() if isinstance(e, dict)}


# ---------------------------------------------------------------------------
# Planner (dry-run is the planner's output, verbatim) and applier. The planner
# reads the store ONLY through the router gateway; the applier writes ONLY
# through Context.store / Context.forget (scrub-gated). Idempotency keys on the
# pk LABEL against the index, and survives content collisions because provenance
# is folded into the stored text (see PromotionCandidate.content).
# ---------------------------------------------------------------------------

def _promoted_index(ctx, scope: RouterScope) -> Dict[str, object]:
    """pk -> live promoted record, via the sole enumeration gateway."""
    idx: Dict[str, object] = {}
    for rec in _spirit_records(ctx, scope):
        lb = dict(rec.labels or {})
        if lb.get("source") == PROMOTE_SOURCE and lb.get("pk"):
            idx[lb["pk"]] = rec
    return idx


def plan_promotion(ctx, scope: RouterScope, candidates: List[PromotionCandidate],
                   ledger: SyncLedger) -> List[PromotionAction]:
    """Typed actions with reasons; no writes. Retired ledger keys are skipped BEFORE
    the store is consulted - a retired source never resurrects, even after its Spirit
    record moved to the recycle bin where no label can testify."""
    existing = _promoted_index(ctx, scope)
    plan: List[PromotionAction] = []
    for c in candidates:
        if ledger.is_retired(c.ledger_key):
            plan.append(PromotionAction("skip", c, reason="source retired"))
            continue
        old = existing.get(c.provenance_key)
        if old is None:
            plan.append(PromotionAction("store", c, reason="new source"))
        elif dict(old.labels or {}).get("content_hash") == c.content_hash:
            plan.append(PromotionAction("skip", c, old_spirit_id=old.id, reason="unchanged"))
        else:
            plan.append(PromotionAction("update", c, old_spirit_id=old.id,
                                        reason="content changed"))
    return plan


def apply_promotion(ctx, plan: List[PromotionAction], ledger: SyncLedger, *,
                    require_identity: bool = True, lock_timeout: float = 30.0,
                    progress: Optional[Callable[[str], None]] = None) -> Dict[str, int]:
    """Execute a plan. Gated on a USABLE workspace identity by default: a copied or
    quarantined workspace may carry the wrong project id, and canonical facts must
    never be minted from it (dry-run/planning stays available - reading is safe).
    Every write is ctx.store / ctx.forget; the seam is never called directly."""
    if require_identity and ctx.identity is None:
        raise PromotionBlocked(
            "promotion requires a usable workspace identity (identity status: %s); "
            "scope one first or pass require_identity=False deliberately"
            % (ctx.identity_view.status,))
    say = progress or (lambda _line: None)
    counts = {"store": 0, "update": 0, "skip": 0, "retire": 0, "error": 0}
    total = len(plan)
    # ONE promotion at a time per store: the base tier is last-write-wins with no lock
    # of its own, so a concurrent promote would silently drop records. Loud, not
    # queued - the second window should know, not wait blind.
    lock_path = ledger.root / LEDGER_DIRNAME / "promote.lock"
    try:
        with exclusive_lock(lock_path, timeout=lock_timeout, what="promotion"):
            for i, a in enumerate(plan, 1):
                c = a.candidate
                try:
                    if a.kind == "store":
                        r = ctx.store(c.content, labels=c.labels(), enrich=False)
                        if r.stored:
                            ledger.mark_active(c, r.id, action="store")
                        say("[%d/%d] store %s: %s" % (i, total, c.source_kind, c.source_record_id))
                    elif a.kind == "update":
                        # store FIRST, forget only after the new record is confirmed:
                        # a store failure must leave the old record live and the
                        # ledger still pointing at it
                        r = ctx.store(c.content, labels=c.labels(), enrich=False)
                        if r.stored:
                            if a.old_spirit_id and a.old_spirit_id != r.id:
                                ctx.forget(a.old_spirit_id)
                            ledger.mark_active(c, r.id, action="update")
                        say("[%d/%d] update %s: %s" % (i, total, c.source_kind, c.source_record_id))
                    elif a.kind == "retire":
                        if a.old_spirit_id:
                            ctx.forget(a.old_spirit_id)
                        ledger.mark_retired(c.ledger_key, pk=c.provenance_key)
                        say("[%d/%d] retire %s: %s" % (i, total, c.source_kind, c.source_record_id))
                    else:
                        say("[%d/%d] skip %s: %s (%s)" % (i, total, c.source_kind,
                                                          c.source_record_id, a.reason))
                    counts[a.kind] = counts.get(a.kind, 0) + 1
                except Exception as exc:             # visible, never silent
                    counts["error"] += 1
                    say("[%d/%d] ERROR %s: %s (%s: %s)" % (i, total, c.source_kind,
                                                           c.source_record_id,
                                                           type(exc).__name__, exc))
    except LockHeld as exc:
        raise PromotionBlocked(str(exc))
    return counts


def verify_ledger(ctx, scope: RouterScope, ledger: SyncLedger) -> List[str]:
    """Reconcile ledger against store. REPORTS drift, never repairs it - the store is
    truth for existence, the ledger for retirement intent, and a silent repair would
    hide whichever one is lying."""
    drift: List[str] = []
    live = _promoted_index(ctx, scope)
    live_ids = {rec.id for rec in live.values()}
    for key, e in ledger.active_keys().items():
        pk = e.get("pk")
        if pk not in live:
            drift.append("ledger active but no live record: %s (pk %s)" % (key, pk))
        elif e.get("current_spirit_id") not in live_ids:
            drift.append("ledger spirit_id stale for %s (pk %s)" % (key, pk))
    ledgered = {e.get("pk") for e in ledger.active_keys().values()}
    for pk, rec in live.items():
        if pk not in ledgered:
            drift.append("unledgered promoted record: pk %s (id %s)" % (pk, rec.id))
    return drift
