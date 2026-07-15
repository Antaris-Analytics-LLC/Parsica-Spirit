"""A reusable conformance suite: assertions ANY StoragePort + AdminBackend must satisfy.

A backend is conformant iff it passes `run_storage_conformance(make_backend)`. The
markdown tier and the engine tier run through this exact suite, so the seam is
proven against real backends - not asserted by a fake shaped to fit. Covers BOTH halves
(storage + the destructive reset ladder), because the safety-critical half is the one
most worth proving identical across backends.
"""

from __future__ import annotations

from typing import Callable

from .record import EnrichmentRecord
from .storage import AdminBackend, PlanError, StoragePort


def run_storage_conformance(make_backend: Callable[[], object]) -> None:
    b = make_backend()
    assert isinstance(b, StoragePort), "backend must satisfy StoragePort"
    assert isinstance(b, AdminBackend), "backend must satisfy AdminBackend"

    # store + get, opaque labels preserved
    rid = b.store("bruxism is teeth grinding at night", labels={"category": "medical", "session": "s1"})
    assert isinstance(rid, str) and rid
    got = b.get(rid)
    assert got is not None and "bruxism" in got.content
    assert got.labels.get("category") == "medical" and got.labels.get("session") == "s1"

    # UPSERT: identical content -> same id, ONE record, labels MERGE (not clobber)
    rid_again = b.store("bruxism is teeth grinding at night", labels={"source": "chat"})
    assert rid_again == rid, "identical content must upsert to the same id"
    merged = b.get(rid)
    assert merged.labels.get("category") == "medical" and merged.labels.get("source") == "chat"

    # plain query
    assert any(r.id == rid for r in b.query("teeth grinding", limit=5))

    # ENRICHMENT queryable - a term only in the enrichment retrieves the record
    rid2 = b.store("the dentist fitted a guard", enrichment=EnrichmentRecord(keywords=("occlusal_splint",)))
    assert any(r.id == rid2 for r in b.query("occlusal_splint", limit=5)), "enrichment must be queryable"

    # LABEL-FILTERED query: opaque AND-match equality, no interpretation
    b.store("apple pie has nothing to do with teeth", labels={"session": "s2"})
    filtered = b.query("teeth", labels={"session": "s1"})
    assert filtered, "label filter must still return the s1 match"
    assert all(r.labels.get("session") == "s1" for r in filtered)

    # MODIFY merges labels (provenance survives), can change enrichment, never content
    assert b.modify(rid, labels={"category": "health"}) is True
    m = b.get(rid)
    assert m.labels.get("category") == "health" and m.labels.get("session") == "s1"  # session preserved
    assert m.content == "bruxism is teeth grinding at night"  # content immutable
    assert b.modify("does-not-exist", labels={"x": "y"}) is False

    # MOVE merges labels
    assert b.move(rid, labels={"shard": "2026-03"}) is True
    assert b.get(rid).labels.get("shard") == "2026-03"

    # empty / no-match queries are safe and empty
    assert b.query("") == []
    assert b.query("zzzznomatchanywhere") == []

    # forget -> recycle bin -> restore; double-forget is False, never a raise
    assert b.forget(rid) is True
    assert b.get(rid) is None
    assert b.forget(rid) is False
    assert rid in {r.id for r in b.recycle_bin()}
    assert b.restore(rid) is True and b.get(rid) is not None

    # --- RESET LADDER: plan -> confirm -> execute is ENFORCED ---
    plan = b.plan_reset(scope="all", disposition="wipe")
    assert plan.affected == len(b.all_records())

    # an unknown hash must NOT wipe - it must raise, and delete nothing
    before = len(b.all_records())
    try:
        b.execute_reset("not-a-real-plan-hash")
        raise AssertionError("execute_reset must reject an unknown plan_hash")
    except PlanError:
        pass
    assert len(b.all_records()) == before

    # a STALE plan (store mutated after planning) must be rejected
    plan2 = b.plan_reset(scope="all", disposition="wipe")
    b.store("a new memory invalidates the pending plan")
    try:
        b.execute_reset(plan2.plan_hash)
        raise AssertionError("execute_reset must reject a stale plan")
    except PlanError:
        pass
    assert len(b.all_records()) > 0

    # the correct flow wipes to the recycle bin (recoverable, not erased)
    plan3 = b.plan_reset(scope="all", disposition="wipe")
    n = b.execute_reset(plan3.plan_hash)
    assert n > 0 and b.all_records() == []
    assert len({r.id for r in b.recycle_bin()}) >= n

    # disposition "save" preserves
    b.store("a fact worth keeping")
    plan_save = b.plan_reset(scope="all", disposition="save")
    b.execute_reset(plan_save.plan_hash)
    assert len(b.all_records()) >= 1

    # migrate_local preserves records (workspace-LOCAL; never an export)
    b.store("migrate me locally")
    pm = b.plan_reset(scope="all", disposition="migrate_local")
    b.execute_reset(pm.plan_hash)
    assert len(b.all_records()) >= 1

    # TWO pending plans: executing one INVALIDATES the others (a reset is itself a mutation)
    p_a = b.plan_reset(scope="all", disposition="wipe")
    p_b = b.plan_reset(scope="all", disposition="wipe")
    b.execute_reset(p_b.plan_hash)
    try:
        b.execute_reset(p_a.plan_hash)
        raise AssertionError("a second pending plan must be invalidated once another executes")
    except PlanError:
        pass

    # restore-widen: a restore between plan and execute changes the id-set -> execute must REJECT
    # (isolates the "any mutation voids a plan" half - restore is the easy-to-miss mutator)
    b.store("rw-keep-record")
    rid_drop = b.store("rw-drop-record")
    b.forget(rid_drop)
    plan_rw = b.plan_reset(scope="all", disposition="wipe")
    before_rw = {r.id for r in b.all_records()}
    b.restore(rid_drop)  # id-set changed since the plan was previewed
    try:
        b.execute_reset(plan_rw.plan_hash)
        raise AssertionError("a reset must reject a plan invalidated by an intervening restore")
    except PlanError:
        pass
    assert {r.id for r in b.all_records()} >= before_rw  # nothing destroyed

    # save-leftover: executing a NON-destructive 'save' plan must still void other pending plans
    # (isolates the "executing a plan voids the others" half - a save preserves the id-set, so a
    #  set-recheck alone would NOT catch a leftover wipe)
    p_wipe = b.plan_reset(scope="all", disposition="wipe")
    p_save = b.plan_reset(scope="all", disposition="save")
    b.execute_reset(p_save.plan_hash)
    try:
        b.execute_reset(p_wipe.plan_hash)
        raise AssertionError("a leftover wipe plan must be voided after another plan executes")
    except PlanError:
        pass

    # modify-voids-plan: a SET-PRESERVING mutation (modify) between plan and execute must still
    # void the plan - this ISOLATES the invalidate-on-mutation backstop, since the set-recheck
    # cannot catch it (modify changes labels, not the id-set)
    target = b.store("a record to relabel mid-plan")
    p_mod = b.plan_reset(scope="all", disposition="wipe")
    b.modify(target, labels={"touched": "yes"})  # id-set unchanged; only invalidate-on-modify catches this
    try:
        b.execute_reset(p_mod.plan_hash)
        raise AssertionError("a set-preserving mutation must still void a pending plan")
    except PlanError:
        pass

    # a narrow scope cannot be planned yet - it must NEVER silently widen to a full wipe
    try:
        b.plan_reset(scope="only-one-session", disposition="wipe")
        raise AssertionError("an unsupported scope must raise, not silently mean 'all'")
    except PlanError:
        pass

    # empty enrichment on re-store must NOT clobber a populated one
    rid_e = b.store("a record with real enrichment", enrichment=EnrichmentRecord(summary="important context"))
    b.store("a record with real enrichment", enrichment=EnrichmentRecord())  # empty placeholder
    ge = b.get(rid_e)
    assert ge.enrichment is not None and ge.enrichment.summary == "important context"

    # VETO BY ABSENCE: no method name hints at export / bundle / portable identity
    for name in dir(b):
        low = name.lower()
        assert not any(bad in low for bad in ("export", "bundle", "portable")), "veto violation: %r" % name

    # determinism: identical query twice yields identical order
    b.store("teeth grinding determinism check")
    assert [r.id for r in b.query("teeth grinding")] == [r.id for r in b.query("teeth grinding")]

    # === recycle-bin lifecycle, boundary, query purity ===

    # RESTORE is lossless - opaque labels AND the FULL enrichment round-trip. Positive control
    # first: prove enrichment fields persist on a plain store, so a later `is None` provably means
    # CLEARED rather than "enrichment never worked".
    rid_rt = b.store("round trip record", labels={"k": "v"},
                     enrichment=EnrichmentRecord(summary="rt summary", keywords=("rtkw",), tags=("rttag",)))
    pre = b.get(rid_rt)
    assert pre.enrichment is not None and pre.enrichment.keywords == ("rtkw",) \
        and pre.enrichment.tags == ("rttag",), "enrichment fields must persist on a plain store (positive control)"
    assert b.forget(rid_rt) is True
    assert b.restore(rid_rt) is True
    rt = b.get(rid_rt)
    assert rt is not None and rt.labels.get("k") == "v", "restore must preserve opaque labels"
    assert rt.enrichment is not None and rt.enrichment.summary == "rt summary" \
        and rt.enrichment.keywords == ("rtkw",) and rt.enrichment.tags == ("rttag",), \
        "restore must preserve the FULL enrichment (summary, keywords, tags)"

    # NO RESURRECTION - forget a record, then store the SAME content fresh: it starts CLEAN and
    # never inherits the tombstone's labels or enrichment (id is content-derived, so they collide)
    rid_z = b.store("zombie content here", labels={"old": "label"},
                    enrichment=EnrichmentRecord(summary="old summary"))
    assert b.forget(rid_z) is True
    rid_z2 = b.store("zombie content here", labels={"new": "label"})  # same content -> same id
    assert rid_z2 == rid_z
    z = b.get(rid_z2)
    assert z.labels.get("new") == "label", "a fresh re-store keeps its own labels"
    assert "old" not in z.labels, "a fresh re-store must NOT resurrect a tombstone's labels"
    assert z.enrichment is None, "a fresh re-store must NOT resurrect a tombstone's enrichment"

    # SAFE RESTORE - once content is re-stored (id is live again), restoring the tombstone must
    # NOT create a duplicate id; it reports it did not restore
    assert b.restore(rid_z) is False, "restore over a live (re-stored) id must be a no-op, not a dup"
    assert len([r for r in b.all_records() if r.id == rid_z]) == 1, "the store must never hold a duplicate id"

    # BOUNDARY - migrate_local must not touch the caller's opaque label namespace AT ALL (exact
    # equality, not just "no key contains 'migrate'": a sentinel under any other key would leak too)
    rid_mg = b.store("a record that will be migrate-flagged", labels={"real": "label", "k2": "v2"})
    before_mg = dict(b.get(rid_mg).labels)
    pmg = b.plan_reset(scope="all", disposition="migrate_local")
    b.execute_reset(pmg.plan_hash)
    mg = b.get(rid_mg)
    assert mg is not None and dict(mg.labels) == before_mg, \
        "migrate_local must leave the caller's labels EXACTLY unchanged (no added/removed/changed keys)"

    # MODIFY enrichment is REPLACE, not merge - a new enrichment fully supplants the prior one
    rid_re = b.store("replace semantics record", enrichment=EnrichmentRecord(keywords=("origkw",)))
    assert b.get(rid_re).enrichment.keywords == ("origkw",)
    assert b.modify(rid_re, enrichment=EnrichmentRecord(summary="newsummary")) is True
    re_ = b.get(rid_re)
    assert re_.enrichment.summary == "newsummary" and re_.enrichment.keywords == (), \
        "modify(enrichment=...) must REPLACE the enrichment wholesale, not merge fields"

    # QUERY PURITY - a pure read: repeated + interleaved queries must not perturb the FULL ordered
    # result. Several records share the ranking token, so any leaked access/recency bump (which the
    # engine tier applies on search) would reorder them and fail full ordered-id equality.
    for i in range(5):
        b.store("purity shared token record number %d here" % i, labels={"n": str(i)})
    q1 = [r.id for r in b.query("purity shared token")]
    assert len(q1) >= 2, "purity stress needs multiple ranked hits"
    for _ in range(3):
        b.query("purity shared token")     # repeat (access bumps would accumulate if impure)
        b.query("unrelated beta probe")    # interleave a different query
    q2 = [r.id for r in b.query("purity shared token")]
    assert q1 == q2, "query must be pure: repeated + interleaved queries must not change the ordered result"
