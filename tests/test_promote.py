"""Promotion model + candidate collection: the provenance-into-content collision
defense, key stability, the isolation/provenance label split, and collection from a
real-shaped Weave project dir (already-scrubbed store files only). The collision
regression here is the promotion twin of
test_claude_memory.test_identical_sections_across_sources_stay_distinct."""
import hashlib
import json
import os
import re
import tempfile

import pytest

from parsica_spirit import open_markdown
from parsica_spirit.promote import (
    PromotionBlocked, PromotionCandidate, PROMOTE_SOURCE, ROUTER_VERSION, SyncLedger,
    apply_promotion, collect_candidates, plan_promotion, verify_ledger,
)
from parsica_spirit.router import RouterScope


def _cand(record_id, body="the retry cap is 60 seconds", kind="lesson", project="proj-alpha-a1b2c3d4e5"):
    return PromotionCandidate(
        scope_id="proj-alpha-a1b2c3d4e5", source_project=project,
        source_kind=kind, source_record_id=record_id, body=body)


def test_render_identical_bodies_different_provenance_differ():
    a = _cand("lesson:2026-07-01:0:aa11bb22")
    b = _cand("lesson:2026-07-02:0:cc33dd44")
    assert a.body == b.body
    assert a.content != b.content, "identical bodies must render to different stored text"
    assert a.provenance_key != b.provenance_key


def test_promote_identical_bodies_different_provenance_survive():
    """CRITICAL regression (base-tier provenance law): the base tier keys record id on
    content and merges labels on collision. Two promoted records sharing a body must
    survive as TWO records with intact provenance."""
    ctx = open_markdown(tempfile.mkdtemp(prefix="pr_coll_"))
    a = _cand("lesson:2026-07-01:0:aa11bb22")
    b = _cand("lesson:2026-07-02:0:cc33dd44")
    ra = ctx.store(a.content, labels=a.labels(), enrich=False)
    rb = ctx.store(b.content, labels=b.labels(), enrich=False)
    assert ra.stored and rb.stored and ra.id != rb.id
    recs = [r for r in ctx.all_records() if dict(r.labels or {}).get("source") == PROMOTE_SOURCE]
    pks = {dict(r.labels)["pk"] for r in recs}
    assert pks == {a.provenance_key, b.provenance_key}, "both provenances must survive"
    for r in recs:
        lb = dict(r.labels)
        assert lb["source_record_id"] in (a.source_record_id, b.source_record_id)


def test_provenance_key_stable_and_body_independent():
    a1 = _cand("journal:2026-07-02:0:ab12cd34", body="first draft")
    a2 = _cand("journal:2026-07-02:0:ab12cd34", body="edited draft")
    assert a1.provenance_key == a2.provenance_key, "an edited body is an UPDATE, same pk"
    assert a1.content_hash != a2.content_hash, "change detection sees the edit"


def test_content_hash_is_pre_promotion_body_hash():
    c = _cand("lesson:2026-07-01:0:aa11bb22")
    assert c.content_hash == hashlib.sha256(c.body.encode("utf-8")).hexdigest()[:16]
    # the rendered (prefixed) text hashes differently - dedup must use the label, and
    # this asserts the label carries the RAW body hash the Weave copy will match
    assert c.content_hash != hashlib.sha256(c.content.encode("utf-8")).hexdigest()[:16]


def test_labels_split_isolation_from_provenance():
    c = _cand("working:pinned:ff00ff00ff00ff00", kind="pinned_fact")
    lb = c.labels()
    assert lb["scope_id"] == "proj-alpha-a1b2c3d4e5"          # isolation
    assert lb["source_project"] == "proj-alpha-a1b2c3d4e5"    # provenance (same value as
    # scope_id in per-project mode, but a distinct KEY)
    assert lb["source"] == PROMOTE_SOURCE
    assert lb["pk"] == c.provenance_key and lb["content_hash"] == c.content_hash
    assert lb["router_version"] == ROUTER_VERSION


def test_ledger_key_shape():
    c = _cand("journal:2026-07-02:1:ab12cd34", kind="journal")
    # provenance-keyed slot: scope_id + the 16-hex pk (which folds source_project in),
    # so two projects sharing a source_record_id take DISTINCT ledger slots.
    assert c.ledger_key == "weave:proj-alpha-a1b2c3d4e5:%s" % c.provenance_key
    assert re.fullmatch(r"weave:proj-alpha-a1b2c3d4e5:[0-9a-f]{16}", c.ledger_key)


def test_ledger_key_is_per_project_distinct():
    """BLOCKER 3 regression: identical scope_id + source_kind + source_record_id but a
    DIFFERENT source_project must NOT collide onto one ledger slot."""
    a = _cand("lesson:2026-07-01:0:aa11bb22", project="projA-1111111111")
    b = _cand("lesson:2026-07-01:0:aa11bb22", project="projB-2222222222")
    assert a.scope_id == b.scope_id and a.source_record_id == b.source_record_id
    assert a.provenance_key != b.provenance_key
    assert a.ledger_key != b.ledger_key, "different projects must not share a ledger slot"


# --- candidate collection from a real-shaped Weave project dir ---------------

WORKING_MD = """# Working set
_Re-injected every SessionStart. Terse and current._

## Pinned facts
<!-- durable: branch, posture, key decisions, hard constraints. -->
- release branch is public, single orphan commit
- the retry cap is 60 seconds
  and it is enforced at the gateway

## Open loops
- finish the router build tonight
"""

LESSONS_APPENDED = """
## 2026-06-30 - shorthand breeds phantom bugs
Reviewers must read real files; embedded shorthand produced findings against code that did not exist.
"""

JOURNAL_0701 = """
## Morning: gateway hardening
Closed both blockers from the review pass.

## Evening: torture test
30x 8-process concurrent run clean.

## Evening: torture test
Re-ran after the lock fix; still clean.
"""


def _weave_project():
    d = tempfile.mkdtemp(prefix="wv_proj-alpha-a1b2c3d4e5_")
    with open(os.path.join(d, "working.md"), "w", encoding="utf-8") as fh:
        fh.write(WORKING_MD)
    # lessons.md = template header (H1 + comment) + appended entries, as pcx note --kind lesson does
    with open(os.path.join(d, "lessons.md"), "w", encoding="utf-8") as fh:
        fh.write("# Lessons / scars\n_Self-authored._\n\n<!-- format:\n## YYYY-MM-DD - title\n-->\n"
                 + LESSONS_APPENDED)
    jd = os.path.join(d, "journal")
    os.makedirs(jd)
    with open(os.path.join(jd, "2026-07-01.md"), "w", encoding="utf-8") as fh:
        fh.write(JOURNAL_0701)
    ed = os.path.join(d, "enrich")
    os.makedirs(ed)
    # two shards; the LATER (spec, at, run_id) overlay must win for chunk c1; c2 has no decisions
    early = {"id": "c1", "enrich": {"spec": "ctx-enrich/v2", "at": "2026-07-01T10:00:00Z",
             "run_id": "r1", "summary": "early summary",
             "decisions": [{"did": "d1", "text": "ship the conservative cut",
                            "conviction": {"value": 0.9, "defaulted": False}}]}}
    late = {"id": "c1", "enrich": {"spec": "ctx-enrich/v2", "at": "2026-07-01T18:00:00Z",
            "run_id": "r2", "summary": "late summary",
            "decisions": [{"did": "d1", "text": "ship the conservative cut",
                           "conviction": {"value": 0.9, "defaulted": False}}]}}
    chatter = {"id": "c2", "enrich": {"spec": "ctx-enrich/v2", "at": "2026-07-01T11:00:00Z",
               "run_id": "r1", "summary": "small talk about the weather", "decisions": []}}
    with open(os.path.join(ed, "2026-07-01.101.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps(early) + "\n" + json.dumps(chatter) + "\n")
    with open(os.path.join(ed, "2026-07-01.202.jsonl"), "w", encoding="utf-8") as fh:
        fh.write(json.dumps(late) + "\n")
    return d


def test_collect_kinds_and_exclusions():
    d = _weave_project()
    cands = collect_candidates(d, scope_id="scope-x")
    by_kind = {}
    for c in cands:
        by_kind.setdefault(c.source_kind, []).append(c)
    assert len(by_kind["pinned_fact"]) == 2
    assert len(by_kind["lesson"]) == 1, \
        "the template comment's example heading must not mint a phantom lesson"
    assert len(by_kind["journal"]) == 3
    assert len(by_kind["enriched_turn"]) == 1
    blob = " ".join(c.body for c in cands)
    assert "finish the router build tonight" not in blob, "open loops never promote"
    assert "durable: branch, posture" not in blob, "template comments are not content"
    assert "small talk" not in blob, "a decision-less overlay never promotes"


def test_collect_wrapped_bullet_folds_and_source_project_defaults_to_dirname():
    d = _weave_project()
    cands = collect_candidates(d, scope_id="scope-x")
    pf = [c for c in cands if c.source_kind == "pinned_fact"]
    assert any("60 seconds and it is enforced" in c.body for c in pf)
    assert all(c.source_project == os.path.basename(d) for c in cands)


def test_collect_is_deterministic_and_ids_stable():
    d = _weave_project()
    a = collect_candidates(d, scope_id="scope-x")
    b = collect_candidates(d, scope_id="scope-x")
    assert [(c.source_record_id, c.content_hash) for c in a] == \
           [(c.source_record_id, c.content_hash) for c in b]
    ids = [c.source_record_id for c in a]
    assert len(ids) == len(set(ids)), "every candidate id is unique"
    assert any(i.startswith("working:pinned:") for i in ids)
    assert any(i.startswith("lesson:2026-06-30:0:") for i in ids), "lesson date parsed from heading"
    jr = [i for i in ids if i.startswith("journal:2026-07-01:")]
    assert len(jr) == 3 and sum(1 for i in jr if ":0:" in i) == 2, \
        "occurrence is per heading path: distinct headings share 0, a duplicate heading gets 1"
    assert any(":1:" in i for i in jr), "the repeated heading is occurrence 1"
    assert "enriched:c1" in ids


def test_collect_latest_overlay_wins_deterministically():
    d = _weave_project()
    cands = collect_candidates(d, scope_id="scope-x")
    turn = [c for c in cands if c.source_kind == "enriched_turn"][0]
    assert "late summary" in turn.body and "early summary" not in turn.body
    assert "ship the conservative cut (conviction 0.9)" in turn.body


def test_collect_survives_malformed_shards():
    d = _weave_project()
    with open(os.path.join(d, "enrich", "2026-07-01.999.jsonl"), "w", encoding="utf-8") as fh:
        fh.write("{not json}\n[]\n" + json.dumps({"id": "c9"}) + "\n")
    cands = collect_candidates(d, scope_id="scope-x")   # must not raise
    assert [c for c in cands if c.source_kind == "enriched_turn"]


def test_tag_is_pii_free_and_survives_scrub():
    """The provenance tag must never trip the scrub gate: hashes, kind, and a
    date-shaped record id only. Store a candidate and assert the tag arrives intact."""
    ctx = open_markdown(tempfile.mkdtemp(prefix="pr_scrub_"))
    c = _cand("journal:2026-07-02:0:ab12cd34", kind="journal")
    r = ctx.store(c.content, labels=c.labels(), enrich=False)
    assert r.stored and r.redactions == 0
    rec = ctx.get(r.id)
    assert rec.content.startswith("[weave %s · journal · journal:2026-07-02:0:ab12cd34"
                                  % c.source_project8)


# --- planner, sync ledger, apply, verify -------------------------------------

SCOPE = RouterScope(scope_id="proj-alpha-a1b2c3d4e5")


def _spirit():
    root = tempfile.mkdtemp(prefix="pr_spirit_")
    ctx = open_markdown(root)
    ctx.scope_identity("test-workspace")
    return root, ctx


def test_plan_then_apply_then_replan_is_idempotent():
    root, ctx = _spirit()
    ledger = SyncLedger(root, SCOPE.scope_id)
    cands = [_cand("lesson:2026-07-01:0:aa11bb22", body="first scar"),
             _cand("journal:2026-07-01:0:cc33dd44", body="first entry", kind="journal")]
    plan = plan_promotion(ctx, SCOPE, cands, ledger)
    assert [a.kind for a in plan] == ["store", "store"]
    assert all(a.reason == "new source" for a in plan)
    counts = apply_promotion(ctx, plan, ledger)
    assert counts["store"] == 2 and counts["error"] == 0
    # re-plan with the SAME sources: everything skips, nothing duplicates
    ctx2 = open_markdown(root)
    plan2 = plan_promotion(ctx2, SCOPE, cands, SyncLedger(root, SCOPE.scope_id))
    assert [a.kind for a in plan2] == ["skip", "skip"]
    assert all(a.reason == "unchanged" for a in plan2)
    n = len([r for r in ctx2.all_records()
             if dict(r.labels or {}).get("source") == PROMOTE_SOURCE])
    assert n == 2


def test_update_forgets_old_and_stores_new_without_duplicates():
    root, ctx = _spirit()
    ledger = SyncLedger(root, SCOPE.scope_id)
    v1 = _cand("enriched:c1", body="summary v1\nDecisions:\n- ship it (conviction 0.8)",
               kind="enriched_turn")
    apply_promotion(ctx, plan_promotion(ctx, SCOPE, [v1], ledger), ledger)
    v2 = _cand("enriched:c1", body="summary v2\nDecisions:\n- ship it (conviction 0.9)",
               kind="enriched_turn")
    plan = plan_promotion(ctx, SCOPE, [v2], ledger)
    assert [a.kind for a in plan] == ["update"] and plan[0].reason == "content changed"
    apply_promotion(ctx, plan, ledger)
    live = [r for r in ctx.all_records()
            if dict(r.labels or {}).get("source") == PROMOTE_SOURCE]
    assert len(live) == 1, "an update must replace, never duplicate"
    assert "summary v2" in live[0].content and "summary v1" not in live[0].content
    assert dict(live[0].labels)["pk"] == v1.provenance_key, "same source, same pk"


def test_retired_source_never_resurrects():
    """The retire-then-recreate scenario the ledger exists for: after retirement the Spirit record
    sits in the recycle bin where no label can testify - only the ledger remembers."""
    root, ctx = _spirit()
    ledger = SyncLedger(root, SCOPE.scope_id)
    c = _cand("lesson:2026-07-01:0:aa11bb22", body="a scar we later retire")
    apply_promotion(ctx, plan_promotion(ctx, SCOPE, [c], ledger), ledger)
    ledger.mark_retired(c.ledger_key, pk=c.provenance_key)
    # retire the live record the way the applier would
    live = [r for r in ctx.all_records()
            if dict(r.labels or {}).get("pk") == c.provenance_key]
    for r in live:
        ctx.forget(r.id)
    # a FRESH ledger instance (new process) must still refuse to bring it back
    plan = plan_promotion(open_markdown(root), SCOPE, [c], SyncLedger(root, SCOPE.scope_id))
    assert [a.kind for a in plan] == ["skip"] and plan[0].reason == "source retired"
    counts = apply_promotion(open_markdown(root), plan, SyncLedger(root, SCOPE.scope_id),
                             require_identity=False)
    assert counts["skip"] == 1 and counts["store"] == 0


def test_ledger_is_content_free_and_sync_ids_monotonic():
    root, ctx = _spirit()
    ledger = SyncLedger(root, SCOPE.scope_id)
    marker = "XDISTINCTIVEBODYTEXTX"
    cands = [_cand("lesson:2026-07-01:0:aa11bb22", body="the %s scar" % marker),
             _cand("journal:2026-07-01:0:cc33dd44", body="the %s entry" % marker,
                   kind="journal")]
    apply_promotion(ctx, plan_promotion(ctx, SCOPE, cands, ledger), ledger)
    text = open(os.path.join(root, ".parsica_router", "promotion_sync.json"),
                encoding="utf-8").read()
    assert marker not in text, "the ledger must never hold free text"
    data = json.loads(text)
    sids = sorted(e["sync_id"] for e in data["records"].values())
    assert sids == sorted(set(sids)) and len(sids) == 2, "sync ids are unique and monotonic"


def test_apply_is_identity_gated_but_planning_is_not():
    root = tempfile.mkdtemp(prefix="pr_noid_")
    ctx = open_markdown(root)                      # no identity scoped
    ledger = SyncLedger(root, SCOPE.scope_id)
    c = _cand("lesson:2026-07-01:0:aa11bb22")
    plan = plan_promotion(ctx, SCOPE, [c], ledger)  # reading is safe: dry-run works
    assert [a.kind for a in plan] == ["store"]
    with pytest.raises(PromotionBlocked):
        apply_promotion(ctx, plan, ledger)
    counts = apply_promotion(ctx, plan, ledger, require_identity=False)
    assert counts["store"] == 1


# --- review fixes (verify pass, 2026-07-03) ---------------------------------

def test_update_store_failure_keeps_old_record_live():
    """review major: update must store-then-forget. A store failure mid-update leaves
    the OLD record live and the ledger still pointing at it - never an orphaned
    recycle-bin record with a stale ledger."""
    root, ctx = _spirit()
    ledger = SyncLedger(root, SCOPE.scope_id)
    v1 = _cand("enriched:c1", body="summary v1", kind="enriched_turn")
    apply_promotion(ctx, plan_promotion(ctx, SCOPE, [v1], ledger), ledger)
    old = [r for r in ctx.all_records()
           if dict(r.labels or {}).get("pk") == v1.provenance_key]
    assert len(old) == 1
    v2 = _cand("enriched:c1", body="summary v2", kind="enriched_turn")
    plan = plan_promotion(ctx, SCOPE, [v2], ledger)
    assert [a.kind for a in plan] == ["update"]
    real_store = ctx.store
    def _boom(*_a, **_k):
        raise RuntimeError("simulated store failure")
    ctx.store = _boom
    try:
        counts = apply_promotion(ctx, plan, ledger)
    finally:
        ctx.store = real_store
    assert counts["error"] == 1 and counts["update"] == 0
    live = [r for r in ctx.all_records()
            if dict(r.labels or {}).get("pk") == v1.provenance_key]
    assert len(live) == 1 and live[0].id == old[0].id, "the old record must stay live"
    assert ledger.entry(v1.ledger_key)["current_spirit_id"] == old[0].id, \
        "the ledger must still point at the surviving record"


def test_concurrent_promotion_is_refused_loudly():
    """review major: the base tier is last-write-wins with no lock, so a second
    concurrent promote must be REFUSED (PromotionBlocked), never silently raced."""
    root, ctx = _spirit()
    ledger = SyncLedger(root, SCOPE.scope_id)
    c = _cand("lesson:2026-07-01:0:aa11bb22")
    plan = plan_promotion(ctx, SCOPE, [c], ledger)
    from parsica_spirit._lock import exclusive_lock
    lock_path = ledger.root / ".parsica_router" / "promote.lock"
    with exclusive_lock(lock_path, timeout=1.0, what="test-holder"):
        with pytest.raises(PromotionBlocked):
            apply_promotion(ctx, plan, ledger, lock_timeout=0.2)
    # holder released: the same plan now applies cleanly
    counts = apply_promotion(ctx, plan, ledger, lock_timeout=1.0)
    assert counts["store"] == 1


def test_instruction_class_has_no_path_into_promotion():
    """The collector must have NO path from Claude Code memory files to a candidate -
    proven functionally AND structurally, so later work cannot silently wire one in."""
    d = tempfile.mkdtemp(prefix="pr_law7_")
    with open(os.path.join(d, "working.md"), "w", encoding="utf-8") as fh:
        fh.write("# Working set\n\n## Pinned facts\n- a real fact\n")
    # Claude Code memory files sitting INSIDE the weave project dir (worst case)
    with open(os.path.join(d, "CLAUDE.md"), "w", encoding="utf-8") as fh:
        fh.write("# Project instructions\n\nUSETABSNOTSPACES directive.\n")
    os.makedirs(os.path.join(d, "memory"))
    with open(os.path.join(d, "memory", "MEMORY.md"), "w", encoding="utf-8") as fh:
        fh.write("# Memory index\n\n- INSTRUCTIONCLASSBODY entry\n")
    cands = collect_candidates(d, scope_id="scope-x")
    blob = " ".join(c.body for c in cands)
    assert "USETABSNOTSPACES" not in blob and "INSTRUCTIONCLASSBODY" not in blob
    from parsica_spirit.promote import SOURCE_KINDS
    assert all(c.source_kind in SOURCE_KINDS for c in cands)
    # structural: the collector never touches the CC-memory discovery surface
    import parsica_spirit.promote as pm
    src = open(pm.__file__, encoding="utf-8").read()
    assert "discover_records" not in src and "import_claude_memory" not in src
    # and the SOURCE_KINDS gate is load-bearing, not decorative
    with pytest.raises(ValueError):
        _cand("x:y", kind="instruction")


def test_collect_applies_current_refusal_list():
    """review fix: a term fenced AFTER capture is redacted at collection - the
    durable floor gets at least the mirror's protection."""
    d = tempfile.mkdtemp(prefix="pr_ref_")
    with open(os.path.join(d, "lessons.md"), "w", encoding="utf-8") as fh:
        fh.write("# Lessons / scars\n\n## 2026-07-01 - a scar\n"
                 "the codenamedZZQPOSTHOCZZClient shipped early.\n")
    cands = collect_candidates(d, scope_id="s", refusal=["zzqposthoczz"])
    blob = " ".join(c.body for c in cands)
    assert "ZZQPOSTHOCZZ" not in blob, "case-insensitive, glued-substring fencing"
    assert "<redacted>" in blob


def test_verify_reports_drift_and_never_repairs():
    root, ctx = _spirit()
    ledger = SyncLedger(root, SCOPE.scope_id)
    c = _cand("lesson:2026-07-01:0:aa11bb22", body="a scar to hand-forget")
    apply_promotion(ctx, plan_promotion(ctx, SCOPE, [c], ledger), ledger)
    assert verify_ledger(ctx, SCOPE, ledger) == []
    # drift: an operator hand-forgets the record outside promotion
    for r in ctx.all_records():
        if dict(r.labels or {}).get("pk") == c.provenance_key:
            ctx.forget(r.id)
    drift = verify_ledger(open_markdown(root), SCOPE, SyncLedger(root, SCOPE.scope_id))
    assert drift and "no live record" in drift[0]
    # and the ledger file is untouched by verification (report, never repair)
    assert SyncLedger(root, SCOPE.scope_id).entry(c.ledger_key)["state"] == "active"
