"""Cross-session recency (the locked contract's retrieval mode), end to end across
the three planes it wires together:

  * adapter (router): federated_hits carries session + created on every hit;
    federated_recent windows on the `created` LABEL (pool clock, the TWO-CLOCKS
    note) with the engine's exclusion semantics - absent tags are INCLUDED,
    never excluded (tags are enhancements, never requirements);
  * seam: the source_session LABEL projects onto MemoryEntry.source_channel;
  * engine: search_by_recency sees the projected field.

The birthday test is the contract's shape: session A stores a fact into the shared
pool, session B retrieves it by recency while excluding its OWN session - one agent
across sessions, live context still per-session."""
import os
import re
import tempfile
import time

import pytest

from parsica_spirit.engine.search import search_by_recency
from parsica_spirit.engine.seam import EngineBackend
from parsica_spirit import open_markdown
from parsica_spirit.promote import PromotionCandidate
from parsica_spirit.router import (
    IsolationViolation, ScopeRequired, federated_hits, federated_recent)

SCOPE = "wsid0123456789abcdef"

#: the ONE plain-dict projection every federated read surfaces (locked shape).
#: source / source_scope / heading_path carry record origin (a Claude Memory import
#: identifies itself at retrieval) - added for the native-memory permanence class.
HIT_KEYS = {"id", "kind", "source_record_id", "content_hash", "body", "session", "created",
            "source", "source_scope", "heading_path"}


def _utc_minute(offset_s=0.0):
    """promote's `created` spelling, offset from now - the label recency keys on."""
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(time.time() + offset_s))


def _root():
    return tempfile.mkdtemp(prefix="rr_store_")


def _store(ctx, body, **labels):
    labels.setdefault("scope_id", SCOPE)
    r = ctx.store(body, labels=labels, enrich=False)
    assert r.stored
    return r.id


# --- federated_hits: session + created on every hit ---------------------------

def test_federated_hits_carry_session_and_created():
    root = _root()
    ctx = open_markdown(root)
    c = PromotionCandidate(scope_id=SCOPE, source_project="proj-a-1122334455",
                           source_kind="lesson",
                           source_record_id="lesson:2026-07-09:0:aa11bb22",
                           body="the recency gateway lesson body",
                           source_session="aaaa1111", event_ts="2026-07-09T18:42Z")
    ctx.store(c.content, labels=c.labels(), enrich=False)
    hits = federated_hits(root, SCOPE, "recency gateway lesson")
    assert len(hits) == 1
    h = hits[0]
    assert set(h) == HIT_KEYS, "the hit projection is the locked read shape"
    assert h["session"] == "aaaa1111", "session is the source_session label, passthrough"
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z$", h["created"]), \
        "created is the created label (store time), passthrough"
    assert h["body"] == "the recency gateway lesson body", "tag stripped for display"


def test_federated_hits_absent_labels_surface_as_empty_strings():
    root = _root()
    ctx = open_markdown(root)
    _store(ctx, "a pre-migration record about widgets")
    hits = federated_hits(root, SCOPE, "widgets")
    assert len(hits) == 1
    assert hits[0]["session"] == "" and hits[0]["created"] == "", \
        "unattributed is empty-string, never hidden and never a KeyError"


# --- federated_recent: the recency window at the adapter plane -----------------

def test_federated_recent_missing_store_returns_empty_and_never_scaffolds():
    root = os.path.join(tempfile.mkdtemp(prefix="rr_fed_"), "does-not-exist")
    assert federated_recent(root, SCOPE) == []
    assert not os.path.exists(root), "a READ must never create the store"


def test_federated_recent_windows_on_the_created_label():
    root = _root()
    ctx = open_markdown(root)
    _store(ctx, "fresh fact, just entered the pool", created=_utc_minute())
    _store(ctx, "ancient fact from another era", created="2020-01-01T00:00Z")
    got = [h["body"] for h in federated_recent(root, SCOPE, hours=12.0)]
    assert got == ["fresh fact, just entered the pool"], \
        "the pool clock is the created label, not the utterance"
    wide = [h["body"] for h in federated_recent(root, SCOPE, hours=1e6)]
    assert set(wide) == {"fresh fact, just entered the pool",
                         "ancient fact from another era"}


def test_federated_recent_absent_or_unparseable_created_never_lands_in_the_window():
    root = _root()
    ctx = open_markdown(root)
    _store(ctx, "no created label at all")
    _store(ctx, "garbage created label", created="not-a-timestamp")
    _store(ctx, "the one real recent record", created=_utc_minute())
    got = [h["body"] for h in federated_recent(root, SCOPE, hours=1e6)]
    assert got == ["the one real recent record"], \
        "records that predate the label are absent from RECENCY (ranked reads still see them)"


def test_federated_recent_orders_newest_first_and_caps_at_limit():
    root = _root()
    ctx = open_markdown(root)
    _store(ctx, "two minutes ago", created=_utc_minute(-120))
    _store(ctx, "right now", created=_utc_minute())
    _store(ctx, "one minute ago", created=_utc_minute(-60))
    got = [h["body"] for h in federated_recent(root, SCOPE, hours=1.0, limit=2)]
    assert got == ["right now", "one minute ago"], \
        "newest first; limit is the K@ budget SHARE, never exceeded"
    assert set(federated_recent(root, SCOPE, hours=1.0, limit=2)[0]) == HIT_KEYS


def test_federated_recent_excludes_the_callers_session_but_never_the_unattributed():
    root = _root()
    ctx = open_markdown(root)
    _store(ctx, "another session's fresh fact", created=_utc_minute(),
           source_session="aaaa1111")
    _store(ctx, "my own session's fresh fact", created=_utc_minute(),
           source_session="bbbb2222")
    _store(ctx, "a pre-migration fresh fact", created=_utc_minute())
    got = {h["body"] for h in federated_recent(root, SCOPE, hours=1.0,
                                               exclude_session="bbbb2222")}
    assert got == {"another session's fresh fact", "a pre-migration fresh fact"}, \
        "exact-equality exclusion; an ABSENT tag is included, never excluded"
    assert len(federated_recent(root, SCOPE, hours=1.0)) == 3, \
        "no exclusion passed -> everything in the window"


def test_federated_recent_tolerates_a_naive_created_as_utc():
    root = _root()
    ctx = open_markdown(root)
    naive = time.strftime("%Y-%m-%dT%H:%M", time.gmtime())   # hand-written, no Z
    _store(ctx, "naive-stamped fresh fact", created=naive)
    got = [h["body"] for h in federated_recent(root, SCOPE, hours=1.0)]
    assert got == ["naive-stamped fresh fact"]


def test_federated_recent_fails_closed_like_every_gateway_read():
    root = _root()
    ctx = open_markdown(root)
    _store(ctx, "a scoped record", created=_utc_minute())
    with pytest.raises(ScopeRequired):
        federated_recent(root, "")
    foreign = _root()
    fctx = open_markdown(foreign)
    _store(fctx, "contaminated record", scope_id="some-other-scope-99")
    with pytest.raises(IsolationViolation):
        federated_recent(foreign, SCOPE)


# --- seam: the label projects onto the engine's recency field ------------------

def test_seam_projects_source_session_and_it_survives_reopen():
    ws = tempfile.mkdtemp(prefix="rr_seam_")
    b1 = EngineBackend(ws)
    rid = b1.store("the alpha fact", labels={"source_session": "aaaa1111"})
    assert b1._find(rid).source_channel == "aaaa1111", "label -> engine field"
    assert b1.get(rid).labels["source_session"] == "aaaa1111", "label stays caller-owned"
    b2 = EngineBackend(ws)
    assert b2._find(rid).source_channel == "aaaa1111", "the field is durable"


def test_seam_modify_backfills_the_field_on_a_pre_migration_record():
    """The backfill tool's write shape: an additive relabel on an untagged record
    must reach the engine field and survive reopen."""
    ws = tempfile.mkdtemp(prefix="rr_backfill_")
    b1 = EngineBackend(ws)
    rid = b1.store("an untagged pre-contract fact")
    assert b1._find(rid).source_channel == ""
    assert b1.modify(rid, labels={"source_session": "pre-migration"})
    assert b1._find(rid).source_channel == "pre-migration"
    assert EngineBackend(ws)._find(rid).source_channel == "pre-migration"


def test_seam_upsert_repoints_the_field_to_the_latest_promoting_session():
    """Re-promotion of identical content re-points the field exactly as the label
    does (last-writer-wins): recency exclusion tracks the MOST RECENT promoting
    session for shared/re-promoted content."""
    ws = tempfile.mkdtemp(prefix="rr_upsert_")
    b = EngineBackend(ws)
    rid1 = b.store("shared promoted content", labels={"source_session": "aaaa1111"})
    rid2 = b.store("shared promoted content", labels={"source_session": "bbbb2222"})
    assert rid1 == rid2, "content-derived id: an upsert, not a duplicate"
    assert b.get(rid1).labels["source_session"] == "bbbb2222"
    assert b._find(rid1).source_channel == "bbbb2222", "the field mirrors the label"


def test_birthday_cross_session_recall():
    """THE BIRTHDAY TEST (locked contract shape): session A promotes a fact into the
    shared pool; session B, moments later, asks 'what just happened elsewhere?' -
    recency window, own session excluded - and gets A's fact plus the unattributed
    pool, never its own echo. Label -> seam projection -> engine field ->
    search_by_recency, end to end."""
    ws = tempfile.mkdtemp(prefix="rr_birthday_")
    a = EngineBackend(ws)                        # session A's engine seam
    a.store("the user's birthday is July 14", labels={"source_session": "sessAAAA"})
    b = EngineBackend(ws)                        # session B, same shared pool
    b.store("session B's own working note", labels={"source_session": "sessBBBB"})
    b.store("an untagged pre-migration fact")
    fresh = EngineBackend(ws)                    # everything crossed disk first
    hits = search_by_recency(fresh._mem.memories, hours=1.0, limit=5,
                             exclude_channel="sessBBBB")
    bodies = [e.content for e in hits]
    assert "the user's birthday is July 14" in bodies, "cross-session recall works"
    assert "session B's own working note" not in bodies, "own session excluded"
    assert "an untagged pre-migration fact" in bodies, "absent tag retrieves (never excluded)"
    who = {e.content: e.source_channel for e in fresh._mem.memories}
    assert who["the user's birthday is July 14"] == "sessAAAA", \
        "the entry knows which session it came from"
