"""Workspace-layout promotion (the locked Workspace Model contract, Spirit side):
collection from the shared workspace docs (lessons.md, journal/ incl. archive, the
per-project working file), per-candidate session provenance (doc stamps and mirror
chunks outrank the promoting session's fallback), and the two-clocks labels
(source_session / created / event_ts). The identity formulas are pinned as LITERALS:
the session threading is a labels-only extension, so provenance_key / content /
content_hash / ledger_key must not depend on it - existing ledgers keep matching or
every already-promoted record double-stores. (ledger_key's SHAPE was reshaped in
BLOCKER 3 to fold source_project in via the pk; it is still labels-only-independent,
which is what the pins here guard.)"""
import inspect
import json
import os
import re
import tempfile
import time
from datetime import datetime

from parsica_spirit.promote import (
    PromotionCandidate, _stamp_provenance, collect_candidates,
)

WS_SCOPE = "wsid0123456789abcdef"
KEY_A = "proj-alpha-a1b2c3d4e5"        # the promoting project (owns working file + enrich)
KEY_B = "proj-bravo-1a2b3c4d5e"        # another project, stamped into the shared docs
KEY_MIGRATED = "proj-charlie-6f7e8d9c0b"
FB = "fbfbfbfb"                        # the promoting session's sess8 (the FALLBACK)

WORKING_WS = """# Working set
_Re-injected every SessionStart. Terse and current._

## Pinned facts
<!-- durable: branch, posture, key decisions, hard constraints. -->
- release branch is public, single orphan commit
- the retry cap is 60 seconds
  and it is enforced at the gateway

## Open loops
- finish the recency build tonight
"""

# workspace lessons.md: template header + stamped entries (law #2), one stamped by
# ANOTHER project's session and one carrying migration's retro stamp.
LESSONS_WS = ("# Lessons / scars\n_Self-authored._\n\n<!-- format:\n## YYYY-MM-DD - title\n-->\n"
              "\n## 2026-07-08 - the stamped scar\n"
              "Reviewers must read real files.\n\n"
              "*(2026-07-09 18:42 UTC · session aaaa1111 · %s)*\n"
              "\n## 2026-06-15 - the migrated scar\n"
              "Old body carried over from the legacy silo.\n\n"
              "*(migrated · %s)*\n" % (KEY_B, KEY_MIGRATED))

JOURNAL_ACTIVE = ("\n## 14:05 checkpoint\n"
                  "Landed the recency read gateway.\n\n"
                  "*(2026-07-09 14:05 UTC · session bbbb2222 · %s)*\n" % KEY_B)

# rotated page: the note verb did not know its session at write time (sess8 '-')
JOURNAL_ARCHIVED = ("\n## 09:00 boot note\n"
                    "Stamped before the session id was known.\n\n"
                    "*(2026-07-01 09:00 UTC · session - · %s)*\n" % KEY_A)


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _overlay_line(cid, at, summary):
    return json.dumps({"id": cid, "enrich": {"spec": "ctx-enrich/v2", "at": at,
                       "run_id": "r1", "summary": summary,
                       "decisions": [{"did": "d1", "text": "ship it",
                                      "conviction": {"value": 0.9, "defaulted": False}}]}})


def _workspace():
    """The NEW Weave layout: shared docs at the root, machine state under
    projects/<key>, capture mirror under mirror/<key> (active + archive/)."""
    ws = tempfile.mkdtemp(prefix="wv_workspace_")
    _write(os.path.join(ws, "lessons.md"), LESSONS_WS)
    _write(os.path.join(ws, "journal", "2026-07-09.md"), JOURNAL_ACTIVE)
    _write(os.path.join(ws, "journal", "archive", "2026-07-01.md"), JOURNAL_ARCHIVED)
    _write(os.path.join(ws, "working", "%s - Working Set.md" % KEY_A), WORKING_WS)
    # mirror: chunk c1 in an active shard, c2 in a ROTATED shard; text never promotes
    _write(os.path.join(ws, "mirror", KEY_A, "2026-07-09.111.jsonl"),
           json.dumps({"id": "c1", "sess": "cccc3333", "ts": "2026-07-09T09:59:00Z",
                       "text": "RAWCHATTERNEVERPROMOTES"}) + "\n")
    _write(os.path.join(ws, "mirror", KEY_A, "archive", "2026-06-01.9.jsonl"),
           json.dumps({"id": "c2", "sess": "dddd4444", "ts": "2026-06-01T08:00:00Z"}) + "\n")
    # enrich overlays: c1 active, c2 rotated to enrich/archive/, c3 has NO mirror chunk
    pdir = os.path.join(ws, "projects", KEY_A)
    _write(os.path.join(pdir, "enrich", "2026-07-09.111.jsonl"),
           _overlay_line("c1", "2026-07-09T10:00:00Z", "recency gateway summary") + "\n"
           + _overlay_line("c3", "2026-07-09T11:00:00Z", "orphan chunk summary") + "\n")
    _write(os.path.join(pdir, "enrich", "archive", "2026-06-01.9.jsonl"),
           _overlay_line("c2", "2026-06-01T08:30:00Z", "rotated overlay summary") + "\n")
    return ws


def _collect_ws(ws, key=KEY_A, sess=FB):
    return collect_candidates(os.path.join(ws, "projects", key), scope_id=WS_SCOPE,
                              source_session=sess, workspace_root=ws)


def _by_rid(cands):
    return {c.source_record_id: c for c in cands}


# --- workspace-mode collection ------------------------------------------------

def test_workspace_collection_finds_all_four_surfaces():
    ws = _workspace()
    cands = _collect_ws(ws)
    by_kind = {}
    for c in cands:
        by_kind.setdefault(c.source_kind, []).append(c)
    assert len(by_kind["pinned_fact"]) == 2, "working/<key> - Working Set.md is the working file"
    assert len(by_kind["lesson"]) == 2, "workspace lessons.md, template comment still inert"
    assert len(by_kind["journal"]) == 2, "active page AND journal/archive/ both collect"
    assert len(by_kind["enriched_turn"]) == 3, "active + ROTATED overlay shards both collect"
    blob = " ".join(c.body for c in cands)
    assert "finish the recency build tonight" not in blob, "open loops never promote"
    assert "RAWCHATTERNEVERPROMOTES" not in blob, "mirror text never promotes (provenance-only join)"


def test_doc_stamps_outrank_the_fallback_session():
    ws = _workspace()
    cands = _collect_ws(ws)
    stamped = [c for c in cands if "stamped scar" in c.body][0]
    assert stamped.source_session == "aaaa1111", "the trailing stamp testifies for its author"
    assert stamped.event_ts == "2026-07-09T18:42Z"
    assert stamped.source_project == KEY_B, "workspace mode re-homes to the stamped project"
    journal = [c for c in cands if "recency read gateway" in c.body][0]
    assert journal.source_record_id.startswith("journal:2026-07-09:0:")
    assert journal.source_session == "bbbb2222" and journal.source_project == KEY_B
    assert journal.event_ts == "2026-07-09T14:05Z"


def test_unknown_stamp_sess_falls_back_but_keeps_the_event_ts():
    ws = _workspace()
    archived = [c for c in _collect_ws(ws) if "boot note" in c.body][0]
    assert archived.source_record_id.startswith("journal:2026-07-01:"), \
        "archive pages keep <date>.md names, so jf.stem is still the date"
    assert archived.source_session == FB, "sess8 '-' means unknown -> the fallback"
    assert archived.event_ts == "2026-07-01T09:00Z"
    assert archived.source_project == KEY_A


def test_migrated_stamp_rehomes_the_project_only():
    ws = _workspace()
    migrated = [c for c in _collect_ws(ws) if "migrated scar" in c.body][0]
    assert migrated.source_project == KEY_MIGRATED, \
        "the retro stamp carries the where; re-homing is what dedups migrated tails"
    assert migrated.source_session == FB and migrated.event_ts == ""


def test_enriched_turn_sess_recovered_from_mirror_incl_archive():
    ws = _workspace()
    rid = _by_rid(_collect_ws(ws))
    c1, c2, c3 = rid["enriched:c1"], rid["enriched:c2"], rid["enriched:c3"]
    assert c1.source_session == "cccc3333" and c1.event_ts == "2026-07-09T09:59:00Z", \
        "chunk ts is carried VERBATIM (second-resolution event clock)"
    assert c2.source_session == "dddd4444" and c2.event_ts == "2026-06-01T08:00:00Z", \
        "rotation's archive/ shards still testify"
    assert c3.source_session == FB and c3.event_ts == "", \
        "a chunk missing from every shard falls back to the promoting session"


def test_pinned_facts_come_from_the_workspace_working_file():
    ws = _workspace()
    pf = [c for c in _collect_ws(ws) if c.source_kind == "pinned_fact"]
    assert any("60 seconds and it is enforced" in c.body for c in pf)
    assert all(c.source_session == FB and c.source_project == KEY_A for c in pf)


def test_cross_project_promotion_renders_identical_shared_doc_candidates():
    """Every promoting project must render a shared-doc entry to the SAME candidate
    (same pk, same content) so the planner + the store's content-derived id dedup it
    instead of double-storing once per project."""
    ws = _workspace()
    docs_a = {c.source_record_id: (c.provenance_key, c.content, c.ledger_key)
              for c in _collect_ws(ws, key=KEY_A, sess=FB)
              if c.source_kind in ("lesson", "journal")}
    docs_b = {c.source_record_id: (c.provenance_key, c.content, c.ledger_key)
              for c in _collect_ws(ws, key=KEY_B, sess="9999ffff")
              if c.source_kind in ("lesson", "journal")}
    assert docs_a and docs_a == docs_b, "shared docs must not double-store per project"


# --- old layout: byte-compatible ----------------------------------------------

def _legacy_project():
    d = tempfile.mkdtemp(prefix="wv_legacy_%s_" % KEY_A)
    _write(os.path.join(d, "working.md"), WORKING_WS)
    _write(os.path.join(d, "lessons.md"), LESSONS_WS)
    _write(os.path.join(d, "journal", "2026-07-09.md"), JOURNAL_ACTIVE)
    _write(os.path.join(d, "enrich", "2026-07-09.111.jsonl"),
           _overlay_line("c1", "2026-07-09T10:00:00Z", "recency gateway summary") + "\n")
    _write(os.path.join(d, "enrich", "archive", "2026-06-01.9.jsonl"),
           _overlay_line("c2", "2026-06-01T08:30:00Z", "rotated overlay summary") + "\n")
    return d


def test_old_layout_identity_is_byte_compatible_across_call_spellings():
    """Weave feature-detects the new kwargs, so BOTH spellings run in the wild - a
    legacy call with and without them must agree on every identity byte."""
    d = _legacy_project()
    old_style = collect_candidates(d, scope_id="scope-x")
    new_style = collect_candidates(d, scope_id="scope-x", source_session="ffff9999",
                                   workspace_root=None)
    key = lambda cs: [(c.source_record_id, c.source_project, c.content,
                       c.provenance_key, c.content_hash, c.ledger_key) for c in cs]
    assert key(old_style) == key(new_style), "identity must not depend on the new kwargs"
    assert all(c.source_session == "" for c in old_style
               if c.source_kind == "pinned_fact"), "no fallback passed -> unknown"
    unstamped = [c for c in new_style if c.source_kind == "pinned_fact"]
    assert all(c.source_session == "ffff9999" for c in unstamped)


def test_old_layout_keeps_stamped_docs_on_the_legacy_project_key():
    """Stamped-project RE-HOMING is gated to workspace mode: legacy docs are
    per-project, and re-keying them would orphan existing ledger entries (pk feeds
    on source_project)."""
    d = _legacy_project()
    cands = collect_candidates(d, scope_id="scope-x", source_session=FB)
    stamped = [c for c in cands if "stamped scar" in c.body][0]
    assert stamped.source_project == os.path.basename(d), "legacy pk identity, byte-for-byte"
    assert stamped.source_session == "aaaa1111", "the stamp still testifies for the WHO"
    assert stamped.event_ts == "2026-07-09T18:42Z"
    assert "enriched:c2" not in {c.source_record_id for c in cands}, \
        "legacy mode never had enrich/archive/ collection; adding it would change output"


# --- pinned identity: the labels-only extension never moves a key --------------

def test_provenance_key_content_and_ledger_key_pinned():
    """Literal pins, computed by the PRE-workspace code: if any of these move,
    every existing ledger stops matching and re-promotes its whole store."""
    c = PromotionCandidate(scope_id="proj-alpha-a1b2c3d4e5",
                           source_project="proj-alpha-a1b2c3d4e5", source_kind="lesson",
                           source_record_id="lesson:2026-07-01:0:aa11bb22",
                           body="the retry cap is 60 seconds",
                           source_session="aaaa1111", event_ts="2026-07-09T18:42Z")
    assert c.provenance_key == "8cc479876cb01186"
    assert c.content_hash == "06de01489505e535"
    assert c.content == ("[weave 2a7f2eee · lesson · lesson:2026-07-01:0:aa11bb22 · "
                         "src:06de0148]\n\nthe retry cap is 60 seconds")
    # BLOCKER 3: the ledger slot is now scope_id + the 16-hex pk (folds source_project
    # in) so two projects sharing a source_record_id no longer collide.
    assert c.ledger_key == "weave:proj-alpha-a1b2c3d4e5:8cc479876cb01186"
    bare = PromotionCandidate(scope_id="proj-alpha-a1b2c3d4e5",
                              source_project="proj-alpha-a1b2c3d4e5", source_kind="lesson",
                              source_record_id="lesson:2026-07-01:0:aa11bb22",
                              body="the retry cap is 60 seconds")
    assert (bare.provenance_key, bare.content, bare.ledger_key) == \
           (c.provenance_key, c.content, c.ledger_key), \
        "session/event_ts are LABELS ONLY - identity is independent of them"


def test_labels_carry_the_two_clocks():
    c = PromotionCandidate(scope_id="s", source_project="p", source_kind="lesson",
                           source_record_id="lesson:2026-07-01:0:aa11bb22", body="b",
                           source_session="aaaa1111", event_ts="2026-07-09T18:42Z")
    lb = c.labels()
    assert lb["source_session"] == "aaaa1111"
    assert lb["event_ts"] == "2026-07-09T18:42Z"
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z$", lb["created"]), \
        "created is store time at minute precision, promote's spelling"
    dt = datetime.fromisoformat(lb["created"].replace("Z", "+00:00"))
    now = datetime.fromisoformat(time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()))
    assert abs((now - dt).total_seconds()) < 120, "created is minted at labels() call"
    bare = PromotionCandidate(scope_id="s", source_project="p", source_kind="lesson",
                              source_record_id="lesson:2026-07-01:0:aa11bb22", body="b")
    lbb = bare.labels()
    assert "source_session" not in lbb and "event_ts" not in lbb, \
        "unknown provenance is ABSENT, never an empty-string label"
    assert "created" in lbb, "the pool clock is unconditional"


def test_signature_supports_weaves_feature_detection():
    params = inspect.signature(collect_candidates).parameters
    for name in ("source_session", "workspace_root"):
        assert name in params, "Weave feature-detects %r via inspect.signature" % name
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert params[name].default is None
    # frozen construction stays fully backward-compatible (new fields after extra_labels)
    c = PromotionCandidate("scope", "proj", "lesson", "lesson:2026-07-01:0:aa11bb22",
                           "body", {})
    assert c.source_session == "" and c.event_ts == ""


# --- the stamp parser ----------------------------------------------------------

def test_stamp_provenance_parses_only_the_trailing_line():
    stamped = ("A scar body.\n\n"
               "*(2026-07-09 18:42 UTC · session aaaa1111 · %s)*" % KEY_B)
    assert _stamp_provenance(stamped) == (KEY_B, "aaaa1111", "2026-07-09T18:42Z")
    quoted = ("The entry QUOTES a stamp:\n"
              "*(2026-07-09 18:42 UTC · session aaaa1111 · %s)*\n"
              "and then keeps talking." % KEY_B)
    assert _stamp_provenance(quoted) == ("", "", ""), "a quoted stamp mid-body never wins"
    assert _stamp_provenance("body\n\n*(2026-07-01 09:00 UTC · session - · %s)*"
                             % KEY_A) == (KEY_A, "", "2026-07-01T09:00Z")
    assert _stamp_provenance("body\n\n*(migrated · %s)*" % KEY_MIGRATED) == \
        (KEY_MIGRATED, "", "")
    assert _stamp_provenance("just a body, no stamp") == ("", "", "")
