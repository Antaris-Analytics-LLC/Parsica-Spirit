"""parsica_spirit.router - the sole read-gateway for router-subsystem Spirit reads.

Cross-scope isolation is fail-CLOSED here because the store gives it no help: the base
label filter matches EVERYTHING on an empty filter (labels_match returns True when the
filter is falsy), Context.query passes a caller's labels straight through, and
all_records() is unfiltered. "Every query carries the scope filter" as a convention is
one forgotten call site away from cross-project bleed, the highest-severity failure in
the router design. So these two functions are the ONLY permitted call sites for
ctx.query / ctx.all_records in the router subsystem (promotion included), enforced by
an AST structural test that DISCOVERS call sites rather than hand-listing them
(tests/test_router.py, the read twin of the write-gateway enforcer pattern).

Scope policy (one engine per project, pinned in the router scope):
  * a scope_id is REQUIRED - absent/blank raises ScopeRequired before any read runs;
  * a record labeled with a DIFFERENT scope_id inside a per-project engine is store
    contamination - IsolationViolation, loud, never a silent drop (better no Spirit
    results than bled results; federation catches this at its boundary and degrades);
  * a record with NO scope label is in-scope: the filesystem boundary owns isolation,
    and pre-router records (Claude Memory imports, hand-stored notes) carry no scope
    label.

`scope_id` is the ISOLATION key; `source_project` is PROVENANCE. Never conflated, so
isolation never keys on a provenance label.
"""

from __future__ import annotations

import calendar
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

ROUTER_VERSION = "1"
SCOPE_LABEL = "scope_id"


class ScopeRequired(ValueError):
    """A Spirit read was attempted without a usable scope - refused before it ran."""


class IsolationViolation(RuntimeError):
    """A Spirit read surfaced records outside the caller's scope - store contamination
    in a per-project engine. Loud by design."""


@dataclass(frozen=True)
class RouterScope:
    """The isolation identity of a router read. scope_id is required and is the ONLY
    key isolation ever tests; the optional fields are context for traces, not gates."""

    scope_id: Optional[str]
    identity_id: Optional[str] = None
    project_root_hash: Optional[str] = None


def _require_scope(scope: RouterScope) -> str:
    sid = (scope.scope_id or "").strip() if isinstance(scope.scope_id, str) else ""
    if not sid:
        raise ScopeRequired("Spirit reads require a non-empty scope_id")
    return sid


def _scope_labels(sid: str, extra: Optional[dict]) -> dict:
    """The query filter with the scope label injected. A caller-supplied conflicting
    scope_id is refused, not silently overwritten - a smuggled scope is a bug worth
    hearing about, not masking."""
    labels = dict(extra or {})
    if SCOPE_LABEL in labels and labels[SCOPE_LABEL] != sid:
        raise IsolationViolation(
            "extra labels carry scope_id %r but the gateway scope is %r"
            % (labels[SCOPE_LABEL], sid))
    labels[SCOPE_LABEL] = sid
    return labels


def _assert_scope(sid: str, records: List) -> List:
    """Verify every returned record against the scope. Unlabeled records pass: the
    filesystem boundary owns isolation and pre-router records (Claude Memory imports,
    hand-stored notes) carry no scope label. A record carrying a DIFFERENT scope_id is
    store contamination by construction - IsolationViolation, loud, never a silent
    drop."""
    out = []
    bad = []
    for rec in records:
        rec_sid = dict(rec.labels or {}).get(SCOPE_LABEL)
        if rec_sid == sid or rec_sid is None:
            out.append(rec)
        elif rec_sid is not None:
            bad.append(rec.id)
    if bad:
        raise IsolationViolation(
            "Spirit returned records outside scope %r: %r" % (sid, bad[:5]))
    return out


def _spirit_query(ctx, scope: RouterScope, text: str, *, limit: int = 8,
                  labels: Optional[dict] = None) -> List:
    """The sole ranked-read door. Queries WITHOUT the scope label in the backend
    filter (exact-match filtering would hide legitimate unlabeled pre-router records)
    and verifies every result instead."""
    sid = _require_scope(scope)
    _scope_labels(sid, labels)                # validates a smuggled scope
    hits = ctx.query(text, limit=limit, labels=(labels or None))
    return _assert_scope(sid, hits)


def _spirit_records(ctx, scope: RouterScope) -> List:
    """The sole full-enumeration door (promotion's idempotency index). Same policy as
    _spirit_query: admits unlabeled pre-router records, foreign labels raise."""
    sid = _require_scope(scope)
    return _assert_scope(sid, ctx.all_records())


# ---------------------------------------------------------------------------
# Federation (the read surface Weave's query.py lazy-imports). Policy lives HERE,
# Spirit-side, so the pure-Weave install carries no dead routing code.
# ---------------------------------------------------------------------------

#: deterministic intent NUDGE: fixed phrase buckets that may reorder LAYER PRECEDENCE
#: in the display, never which records are returned. English-only and brittle by
#: nature, so they are a nudge, never a gate; both-or-neither falls to the default.
RECENT_HINTS = ("today", "this session", "just now", "recent", "latest",
                "what did we just", "current state", "open loop")
LONG_HORIZON_HINTS = ("previously", "last time", "we decided", "standing decision",
                      "historically", "long term", "across sessions",
                      "what do you remember")

_TAG_PREFIXES = ("[weave ", "[claude ")


def layer_order(query_text: str) -> Tuple[str, str]:
    """Display precedence for a federated query: recency questions lead with the warm
    layer, long-horizon questions lead with the durable floor. Deterministic, pure."""
    t = (query_text or "").lower()
    recent = any(h in t for h in RECENT_HINTS)
    durable = any(h in t for h in LONG_HORIZON_HINTS)
    if durable and not recent:
        return ("spirit", "weave")
    return ("weave", "spirit")


def strip_provenance_tag(content: str) -> str:
    """Drop the leading machine provenance tag ([weave ...] / [claude ...]) for
    display - the labels carry the same identity; the tag exists for the store's
    content-derived id, not for human eyes."""
    for p in _TAG_PREFIXES:
        if content.startswith(p) and "\n\n" in content:
            return content.split("\n\n", 1)[1]
    return content


def _hit_dict(r) -> dict:
    """The one plain-dict projection of a Spirit record for federation callers, so
    every federated read surfaces the same keys. "session" is the source_session
    label (empty when the record predates session tagging - unattributed, never
    hidden) and "created" is the created label (store time, the pool's clock).
    "source"/"source_scope"/"heading_path" carry the record's origin (e.g. a Claude
    Memory import identifies itself), so the display layer can render provenance and
    collapse a record against other copies of the same content."""
    lb = dict(r.labels or {})
    return {
        "id": r.id,
        "kind": lb.get("source_kind") or lb.get("record_class") or "record",
        "source_record_id": lb.get("source_record_id") or "",
        "content_hash": lb.get("content_hash") or "",
        "body": strip_provenance_tag(r.content),
        "session": lb.get("source_session") or "",
        "created": lb.get("created") or "",
        "source": lb.get("source") or "",
        "source_scope": lb.get("source_scope") or "",
        "heading_path": lb.get("heading_path") or "",
    }


def federated_hits(spirit_root, scope_id: str, text: str, *, limit: int = 10) -> List[dict]:
    """Read-only federation entry. Returns [] when the Spirit store does not exist -
    a READ NEVER SCAFFOLDS (the promote verb is the only thing that creates the
    store). Plain dicts so the Weave caller needs no Spirit types. Raises on real
    failures; the caller's boundary degrades to Weave-only."""
    root = Path(spirit_root)
    if not (root.exists() and root.is_dir()):
        return []
    from .context import open_markdown          # local: keep router import-light
    ctx = open_markdown(str(root), read_only=True)   # federated read: no lock, no scaffold, no footprint
    hits = _spirit_query(ctx, RouterScope(scope_id=scope_id), text, limit=limit)
    return [_hit_dict(r) for r in hits]


def _utc_now() -> datetime:
    """The recency window's clock - the ONE deliberate wall-clock read in this
    module. A recency read is time-relative BY CONTRACT ("what just entered the
    pool" cannot be answered clock-free); ranked reads stay deterministic and
    clock-free, which is what the wall-clock enforcer guards. Spelled via
    time.gmtime, the same UTC source promote's `created` stamp uses."""
    return datetime.fromtimestamp(calendar.timegm(time.gmtime()), tz=timezone.utc)


def federated_recent(spirit_root, scope_id: str, *, hours: float = 12.0, limit: int = 5,
                     exclude_session: Optional[str] = None) -> List[dict]:
    """Read-only RECENCY federation entry: the records whose `created` label (store
    time - when the record entered the pool, the TWO-CLOCKS note's pool clock) falls
    inside the last `hours`, newest first, at most `limit`. Mirrors the engine's
    search_by_recency semantics at the adapter plane: a retrieval mode, not a
    tiebreak. Same contract as federated_hits - returns []
    when the Spirit store does not exist (a READ NEVER SCAFFOLDS), plain dicts,
    raises on real failures so the caller's boundary degrades to Weave-only.

    Exclusion mirrors the engine's exclude_channel: when `exclude_session` is set,
    records whose source_session label EQUALS it are skipped; records with an
    ABSENT session label are always INCLUDED (unattributed, never excluded - a
    pre-migration record still retrieves; tags are enhancements, never
    requirements). A naive `created` timestamp is tolerated as UTC; an absent or
    unparseable one simply never lands in the window.

    BUDGET (the K@ note): `limit` is the caller's SHARE of its total retrieval
    budget, never an add-on - recency slots come OUT of the budget, so a K@15
    caller giving recency 5 slots shows 5 recent + 10 ranked (5+10, not 5+15).
    This function never returns more than `limit` and never inflates a ranked
    read additively."""
    root = Path(spirit_root)
    if not (root.exists() and root.is_dir()):
        return []
    from .context import open_markdown          # local: keep router import-light
    ctx = open_markdown(str(root), read_only=True)   # federated read: no lock, no scaffold, no footprint
    records = _spirit_records(ctx, RouterScope(scope_id=scope_id))
    cutoff = _utc_now() - timedelta(hours=hours)
    recent: List[tuple] = []
    for rec in records:
        lb = dict(rec.labels or {})
        created = lb.get("created") or ""
        if not created:
            continue
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)    # tolerate naive = UTC
        if dt < cutoff:
            continue
        if exclude_session and (lb.get("source_session") or "") == exclude_session:
            continue
        recent.append((dt, rec))
    recent.sort(key=lambda pair: pair[0], reverse=True)  # newest first; stable ties
    return [_hit_dict(rec) for _, rec in recent[:limit]]
