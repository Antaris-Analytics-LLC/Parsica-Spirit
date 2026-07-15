"""Full-lifecycle smoke test for the markdown BASE tier - the engine is not involved.

Proves the markdown base tier is complete and honest on its own: a scrub-gated store,
query, durable JSONL persistence, and the reset ladder, all on markdown alone (the engine
ships in the box but is never wired here). The "engine never imported by the base tier"
guarantee is enforced separately by test_seam_guards (import-direction) and the clean-room
CI install (stage 1, engine-absent).
"""

import json
import os
import tempfile

from parsica_spirit import Context, open_markdown


def test_tier_diagnostic_reports_markdown():
    ctx = open_markdown(tempfile.mkdtemp(prefix="pcx_tier_"))
    assert ctx.tier == "MarkdownBackend"
    assert isinstance(ctx, Context)


def test_scrub_gate_redacts_before_storage():
    ctx = open_markdown(tempfile.mkdtemp(prefix="pcx_scrub_"))
    res = ctx.store(
        "deploy note: ANTHROPIC api_key = sk-ant-AAAAAAAAAAAAAAAAAAAAAAAA - also notes on bruxism",
        labels={"category": "ops"},
    )
    assert res.redactions >= 1, "the scrub gate must redact the planted secret"
    got = ctx.get(res.id)
    assert got is not None
    assert "sk-ant-" not in got.content, "a secret must NEVER reach stored markdown"
    assert "[REDACTED" in got.content
    # the enrichment is derived from the CLEAN text, so it cannot carry the secret either
    if got.enrichment is not None:
        assert "sk-ant-" not in " ".join(got.enrichment.keywords + got.enrichment.tags + (got.enrichment.summary,))


def test_full_lifecycle_markdown_only():
    root = tempfile.mkdtemp(prefix="pcx_life_")
    ctx = open_markdown(root)

    res = ctx.store("bruxism is teeth grinding at night", labels={"category": "medical"})
    assert res.redactions == 0
    assert any(h.id == res.id for h in ctx.query("teeth grinding")), "stored memory must be queryable"

    # durability: a fresh Context over the same root sees the record (JSONL persistence)
    assert open_markdown(root).get(res.id) is not None

    # relabel (modify) is visible
    assert ctx.relabel(res.id, labels={"reviewed": "yes"}) is True
    assert ctx.get(res.id).labels.get("reviewed") == "yes"

    # reset ladder: plan -> execute(wipe) -> recycle -> restore (recoverable, not erased)
    ctx.store("a second memory")
    plan = ctx.plan_reset(scope="all", disposition="wipe")
    n = ctx.execute_reset(plan.plan_hash)
    assert n >= 2 and ctx.all_records() == []
    assert res.id in {r.id for r in ctx.recycle_bin()}
    assert ctx.recover(res.id) is True and ctx.get(res.id) is not None


# --- base-tier regression tests ---

def test_store_empty_is_not_persisted():
    ctx = open_markdown(tempfile.mkdtemp(prefix="pcx_empty2_"))
    assert ctx.store("   ").stored is False        # whitespace-only
    assert ctx.store("").stored is False
    assert ctx.store("   ").id == ""
    assert ctx.all_records() == [], "an empty payload must not create a junk record"


def test_enrich_false_skips_enricher_but_scrub_still_runs():
    ctx = open_markdown(tempfile.mkdtemp(prefix="pcx_enr_"))
    res = ctx.store("secret api_key=sk-ant-AAAAAAAAAAAAAAAAAAAAAAAA plus real notes on bruxism", enrich=False)
    assert res.stored and res.redactions >= 1
    got = ctx.get(res.id)
    assert got.enrichment is None, "enrich=False must skip the enricher"
    assert "sk-ant-" not in got.content, "the scrub gate still runs regardless of enrich"


def test_open_markdown_exposes_continuity_sharing_one_root():
    root = tempfile.mkdtemp(prefix="pcx_uni_")
    ctx = open_markdown(root)
    assert ctx.continuity is not None
    ctx.store("a stored memory about teeth grinding")
    ctx.continuity.save_working_set("## Pinned\n- Project: Parsica Spirit\n")
    # store (records.jsonl) and continuity (working.md) share the one root
    ctx2 = open_markdown(root)
    assert ctx2.query("teeth grinding"), "the record persists under the shared root"
    assert "Parsica Spirit" in ctx2.continuity.restore(), "the working set persists under the shared root"


def test_labels_are_scrub_gated_on_store_and_relabel():
    # labels are an on-disk write path too: a secret in a label KEY or VALUE must be scrubbed, counted
    # in WriteResult.redactions, and never persisted raw to records.jsonl (store AND relabel).
    root = tempfile.mkdtemp(prefix="pcx_lblscrub_")
    ctx = open_markdown(root)
    res = ctx.store("a clean note about bruxism",
                    labels={"owner": "api_key=sk-ant-AAAAAAAAAAAAAAAAAAAAAAAA", "note": "ok"})
    assert res.stored and res.redactions >= 1, "a secret in a label must be counted as a redaction"
    raw = open(os.path.join(root, "records.jsonl"), encoding="utf-8").read()
    assert "sk-ant-" not in raw, "a secret in a label value must never reach records.jsonl"
    got = ctx.get(res.id)
    assert "sk-ant-" not in " ".join(list(got.labels.keys()) + list(got.labels.values()))
    # a secret hidden in a label KEY is scrubbed too
    ctx.store("another note", labels={"api_key=sk-ant-BBBBBBBBBBBBBBBBBBBBBBBB": "v"})
    assert "sk-ant-B" not in open(os.path.join(root, "records.jsonl"), encoding="utf-8").read(), \
        "a secret in a label key must be scrubbed"
    # relabel is scrub-gated as well
    ctx.relabel(res.id, labels={"reviewed": "token=sk-ant-CCCCCCCCCCCCCCCCCCCCCCCC"})
    assert "sk-ant-C" not in open(os.path.join(root, "records.jsonl"), encoding="utf-8").read(), \
        "a secret in a relabel value must be scrubbed"


def test_cross_context_reads_refresh_from_disk():
    # the "works until I open two terminals" trap: a second long-lived Context on the same workspace
    # must see the first's committed writes WITHOUT having to write itself (reads refresh from disk).
    root = tempfile.mkdtemp(prefix="pcx_xctx_")
    a = open_markdown(root)
    b = open_markdown(root)
    a.store("alpha beta gamma")
    hits = b.query("alpha")
    assert len(hits) == 1 and "alpha" in hits[0].content, "second context sees the first's write on read"
    # returned records are defensive copies: mutating one must not corrupt the store
    hits[0].labels["injected"] = "x"
    assert "injected" not in (b.query("alpha")[0].labels or {}), "reads return copies, not internal objects"


def test_nested_label_value_is_refused_never_persisted():
    # a scrubber that only walked the top level would let a secret buried in a nested value reach
    # records.jsonl raw. Labels are strict scalars: a container is refused BEFORE anything is written.
    import pytest
    root = tempfile.mkdtemp(prefix="pcx_lblnest_")
    ctx = open_markdown(root)
    with pytest.raises(TypeError):
        ctx.store("x", labels={"meta": {"secret": "sk-ant-" + "A" * 24}})
    # refused before any disk write: no records.jsonl, or one that never saw the secret
    p = os.path.join(root, "records.jsonl")
    assert (not os.path.exists(p)) or "sk-ant-" not in open(p, encoding="utf-8").read()
    with pytest.raises(TypeError):
        ctx.store("y", labels={"tags": ["sk-ant-" + "B" * 24]})   # list value also refused


def test_non_scalar_label_key_and_value_types_rejected():
    import pytest
    root = tempfile.mkdtemp(prefix="pcx_lbltype_")
    ctx = open_markdown(root)
    with pytest.raises(TypeError):
        ctx.store("x", labels={123: "value"})                     # non-string key
    with pytest.raises(ValueError):
        ctx.store("x", labels={"score": float("nan")})            # non-finite float
    # scalars still work end to end
    r = ctx.store("ok", labels={"n": 3, "flag": True, "ratio": 0.5, "note": "hi", "none": None})
    assert r.stored


def test_label_scrub_preserves_query_roundtrip():
    # scrubbing labels must NOT break a store->query round-trip: query() applies the SAME transform,
    # so non-str values keep their type and a PII/secret-shaped value still matches its own filter.
    import pytest
    root = tempfile.mkdtemp(prefix="pcx_lblrt_")
    ctx = open_markdown(root)

    # non-str label: type preserved (not stringified), and query by the same value matches
    rb = ctx.store("bool note", labels={"archived": True})
    assert ctx.get(rb.id).labels.get("archived") is True, "a bool label keeps its type (no str() coercion)"
    assert any(h.id == rb.id for h in ctx.query("bool", labels={"archived": True})), "bool label filters"

    # PII-shaped value: scrubbed on disk, yet a query by the ORIGINAL value still matches (symmetric)
    rp = ctx.store("pii note", labels={"assignee": "bob@example.com"})
    assert "bob@example.com" not in open(os.path.join(root, "records.jsonl"), encoding="utf-8").read()
    assert any(h.id == rp.id for h in ctx.query("pii", labels={"assignee": "bob@example.com"})), \
        "a redacted label value is still queryable by its original value (store/query symmetric)"

    # plain string label unaffected
    rs = ctx.store("plain note", labels={"cat": "ops"})
    assert any(h.id == rs.id for h in ctx.query("plain", labels={"cat": "ops"}))

    # two keys that scrub to the same placeholder must RAISE, never silently drop a label
    with pytest.raises(ValueError):
        ctx.store("collide", labels={"k_sk-ant-AAAAAAAAAAAAAAAAAAAAAAAA": "1",
                                     "k_sk-ant-BBBBBBBBBBBBBBBBBBBBBBBB": "2"})


# --- Audit trail (metadata-only) + migrate-is-provably-local ---

def test_audit_trail_is_metadata_only_and_records_destructive_ops():
    # The facade logs destructive / identity ops to <root>/audit.jsonl. It records the op + the facade's
    # timestamp + minimal metadata (a record id, an affected count) - NEVER the content or labels it
    # destroyed. An audit that stored what it erased would defeat the erase.
    root = tempfile.mkdtemp(prefix="pcx_audit_")
    ctx = open_markdown(root)
    r1 = ctx.store("memory one about bruxism", labels={"category": "medical"})
    r2 = ctx.store("memory two, also secret-free")
    assert ctx.audit_log() == [], "a store is not a destructive op - no audit noise"

    assert ctx.forget(r1.id) is True
    plan = ctx.plan_reset(scope="all", disposition="wipe")
    n = ctx.execute_reset(plan.plan_hash)   # wipes the one remaining live record (r2)

    log = ctx.audit_log()
    ops = [e["op"] for e in log]
    assert "forget" in ops and "execute_reset" in ops, "destructive ops are audited"
    assert all("ts" in e and "op" in e for e in log), "every entry carries a facade timestamp + op"
    assert any(e["op"] == "execute_reset" and e.get("affected") == n for e in log)
    # metadata-only: neither content nor a label value/key ever appears in the trail
    blob = json.dumps(log)
    assert "bruxism" not in blob, "audit must never carry destroyed CONTENT"
    assert "medical" not in blob, "audit must never carry a destroyed LABEL"
    # the trail is durable + append-only across Context instances over the same root
    assert open_markdown(root).audit_log() == log, "the audit trail persists under the shared root"


def test_migrate_local_is_provably_local_by_veto_of_absence():
    # "migrate is provably local" is enforced by ABSENCE: nowhere on the reset surface is there a
    # destination parameter, nor a **kwargs to smuggle one through. migrate_local can therefore only
    # ever act within THIS workspace - there is no channel to name another one.
    import inspect
    from parsica_spirit.enrich import AdminBackend
    from parsica_spirit.backends.markdown import MarkdownBackend
    banned = {"path", "root", "dest", "destination", "uri", "url", "host",
              "target", "to", "src", "source", "remote", "endpoint"}
    surfaces = [AdminBackend.plan_reset, AdminBackend.execute_reset,
                AdminBackend.recycle_bin, AdminBackend.restore,
                MarkdownBackend.plan_reset, MarkdownBackend.execute_reset]
    for fn in surfaces:
        sig = inspect.signature(fn)
        for pname, p in sig.parameters.items():
            assert p.kind is not inspect.Parameter.VAR_KEYWORD, \
                "no **kwargs may smuggle a destination into %s" % fn.__qualname__
            assert pname.lower() not in banned, \
                "the reset surface must expose NO destination param (%r on %s)" % (pname, fn.__qualname__)


def test_migrate_local_preserves_in_place_and_never_touches_another_workspace():
    # Behavioral proof of locality: migrate_local preserves THIS workspace's records in place, and a
    # second, independent workspace is wholly untouched - no record crosses the boundary.
    a_root = tempfile.mkdtemp(prefix="pcx_mla_")
    b_root = tempfile.mkdtemp(prefix="pcx_mlb_")
    a = open_markdown(a_root)
    b = open_markdown(b_root)
    ra = a.store("a memory that lives only in A")
    rb = b.store("a memory that lives only in B")

    plan = a.plan_reset(scope="all", disposition="migrate_local")
    a.execute_reset(plan.plan_hash)

    assert a.get(ra.id) is not None, "migrate_local preserves A's records in place (local, non-destructive)"
    assert b.get(rb.id) is not None, "B is a different workspace and is wholly untouched"
    a_blob = open(os.path.join(a_root, "records.jsonl"), encoding="utf-8").read()
    assert "lives only in B" not in a_blob, "no record from B may ever appear in A"


def test_open_markdown_expands_home(monkeypatch, tmp_path):
    # ~ must expand so the store / continuity / identity all share a resolved (non-literal-~) root
    home = str(tmp_path)
    monkeypatch.setenv("USERPROFILE", home)   # Windows
    monkeypatch.setenv("HOME", home)          # POSIX
    ctx = open_markdown("~/parsica_home_test")
    ctx.store("a memory under the expanded home")
    ctx.scope_identity("HomeAgent")
    resolved = os.path.join(home, "parsica_home_test")
    assert os.path.isdir(resolved), "~ must expand to the home dir, not a literal ./~"
    assert os.path.exists(os.path.join(resolved, "records.jsonl")), "store lands under the expanded root"
    assert os.path.exists(os.path.join(resolved, "identity.json")), "identity lands under the expanded root"
