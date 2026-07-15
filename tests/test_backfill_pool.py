"""scripts/backfill_pool.py - pool backfill (three tiers) + scope merge, driven the
way the operator runs it: as a subprocess, against temp roots. The whole scenario
runs ONCE in a module fixture (build -> backfill x2 -> refusal -> merge x2) and the
tests assert its facets: tier joins across mirror layouts incl. archive, additive
idempotency (byte-identical store on rerun), ledger continuity (active re-keyed,
retirement carried, destination wins a conflict), the marker short-circuit, and
file conservation - nothing unlinked, ever."""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from parsica_spirit import open_markdown
from parsica_spirit.promote import PromotionCandidate, SyncLedger, verify_ledger
from parsica_spirit.router import RouterScope, _spirit_records

_REPO = Path(__file__).resolve().parents[1]
_TOOL = _REPO / "scripts" / "backfill_pool.py"
_SRC = str(_REPO / "src")
_ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
        "PYTHONPATH": _SRC + os.pathsep + os.environ.get("PYTHONPATH", "")}

OLD, NEW = "scopeold", "scopenew"


def _run(*argv):
    p = subprocess.run([sys.executable, str(_TOOL)] + list(argv), env=_ENV,
                       capture_output=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _by_srid(base, scope):
    ctx = open_markdown(str(base / scope))
    recs = _spirit_records(ctx, RouterScope(scope_id=scope))
    return {dict(r.labels or {}).get("source_record_id", ""): r for r in recs}


def _lb(base, scope, srid):
    return dict(_by_srid(base, scope)[srid].labels or {})


def _cand(scope, kind, srid, body, project="projA-1234567890", **kw):
    return PromotionCandidate(scope_id=scope, source_project=project,
                              source_kind=kind, source_record_id=srid, body=body, **kw)


@pytest.fixture(scope="module")
def pool():
    base = Path(tempfile.mkdtemp(prefix="pool_bf_"))
    weave = base / "weave"

    # fake Weave mirrors: workspace layout (active shard + rotation's archive/) and a
    # LEGACY-layout project - tier (a) must join chunks across every project + layout
    mproj = weave / "mirror" / "projA-1234567890"
    (mproj / "archive").mkdir(parents=True)
    (mproj / "2026-07-09.111.jsonl").write_text(json.dumps(
        {"id": "chunk123", "sess": "sessAAAA", "ts": "2026-07-09T10:00:00Z",
         "text": "never read"}) + "\n", encoding="utf-8")
    (mproj / "archive" / "2026-06-01.9.jsonl").write_text(json.dumps(
        {"id": "chunk456", "sess": "sessBBBB", "ts": "2026-06-01T08:00:00Z"}) + "\n",
        encoding="utf-8")
    lproj = weave / "projB-0987654321" / "mirror"
    lproj.mkdir(parents=True)
    (lproj / "2026-05-05.7.jsonl").write_text(json.dumps(
        {"id": "chunk789", "sess": "sessCCCC", "ts": "2026-05-05T05:05:00Z"}) + "\n",
        encoding="utf-8")

    # OLD scope: six pre-contract records (session/clock labels stripped, exactly what
    # a pre-upgrade promote left behind) + ONE post-contract record with a pinned
    # created label, to prove merge preserves the pool clock verbatim
    cands = [
        _cand(OLD, "enriched_turn", "enriched:chunk123",
              "Summary A.\nDecisions:\n- decision one"),
        _cand(OLD, "enriched_turn", "enriched:chunk456",
              "Summary B.\nDecisions:\n- decision two"),
        _cand(OLD, "enriched_turn", "enriched:chunk789",
              "Summary C.\nDecisions:\n- decision three", project="projB-0987654321"),
        _cand(OLD, "lesson", "lesson:2026-07-01:0:deadbeef",
              "A lesson\n\nnever push unreviewed"),
        _cand(OLD, "journal", "journal:undated:0:0badf00d", "Undated journal fragment"),
        _cand(OLD, "pinned_fact", "working:pinned:cafebabecafebabe",
              "the store root is sacred"),
    ]
    tagged = _cand(OLD, "journal", "journal:2026-07-05:0:c0ffee00",
                   "Post-contract entry, fully tagged",
                   source_session="eeee5555", event_ts="2026-07-05T11:59:00Z")
    ctx_old = open_markdown(str(base / OLD))
    led_old = SyncLedger(base / OLD, OLD)
    for c in cands:
        lb = c.labels()
        for k in ("source_session", "created", "event_ts"):
            lb.pop(k, None)                     # simulate a pre-contract record
        r = ctx_old.store(c.content, labels=lb, enrich=False)
        led_old.mark_active(c, r.id, action="store")
    lb = tagged.labels()
    lb["created"] = "2026-07-05T12:00Z"         # pin the pool clock for the verbatim check
    r = ctx_old.store(tagged.content, labels=lb, enrich=False)
    led_old.mark_active(tagged, r.id, action="store")
    # retirement intent with NO live record: only the ledger remembers; must carry
    led_old.mark_retired("weave:%s:journal:journal:2026-01-01:0:feedface" % OLD,
                         pk="cafe" * 4)
    assert verify_ledger(ctx_old, RouterScope(scope_id=OLD),
                         SyncLedger(base / OLD, OLD)) == []

    r = {"base": base, "weave": weave, "cands": cands + [tagged]}

    r["backfill1"] = _run("backfill", "--spirit-root", str(base), "--scope", OLD,
                          "--weave-root", str(weave))
    r["snap_bf1"] = (base / OLD / "records.jsonl").read_bytes()
    r["backfill2"] = _run("backfill", "--spirit-root", str(base), "--scope", OLD,
                          "--weave-root", str(weave))
    r["snap_bf2"] = (base / OLD / "records.jsonl").read_bytes()
    r["typo"] = _run("backfill", "--spirit-root", str(base), "--scope", OLD,
                     "--weave-root", str(base / "nope"))

    # pre-seed NEW: (1) a content-identical copy of the lesson (shared docs promote
    # identically from every scope - the dedup path); (2) the SAME journal source with
    # a DIFFERENT body, ledger-tracked (the conflict path: destination wins)
    ctx_new = open_markdown(str(base / NEW))
    led_new = SyncLedger(base / NEW, NEW)
    dup = _cand(NEW, "lesson", "lesson:2026-07-01:0:deadbeef",
                "A lesson\n\nnever push unreviewed")
    dlb = dup.labels()
    for k in ("source_session", "created", "event_ts"):
        dlb.pop(k, None)
    r["dup_id"] = ctx_new.store(dup.content, labels=dlb, enrich=False).id
    conf = _cand(NEW, "journal", "journal:undated:0:0badf00d",
                 "Undated journal fragment REVISED IN NEW")
    r["conf_id"] = ctx_new.store(conf.content, labels=conf.labels(), enrich=False).id
    led_new.mark_active(conf, r["conf_id"], action="store")

    r["before_files"] = {p for p in (base / OLD).rglob("*") if p.is_file()}
    r["old_store_snap"] = (base / OLD / "records.jsonl").read_bytes()
    r["merge1"] = _run("merge-scope", "--spirit-root", str(base),
                       "--from", OLD, "--into", NEW)
    r["after_files"] = {p for p in (base / OLD).rglob("*") if p.is_file()}
    r["old_store_after"] = (base / OLD / "records.jsonl").read_bytes()
    r["new_snap_m1"] = (base / NEW / "records.jsonl").read_bytes()
    r["merge2"] = _run("merge-scope", "--spirit-root", str(base),
                       "--from", OLD, "--into", NEW)
    r["new_snap_m2"] = (base / NEW / "records.jsonl").read_bytes()
    return r


# --- backfill: three tiers ------------------------------------------------------

def test_backfill_run_is_ok_and_manifest_counts_the_tiers(pool):
    rc, out = pool["backfill1"]
    assert rc == 0, out
    assert "tier a 3 | tier b 2 | tier c 1 | skipped 1 | errors 0" in out, out


def test_tier_a_joins_the_mirror_across_layouts_and_archive(pool):
    base = pool["base"]
    lb = _lb(base, OLD, "enriched:chunk123")
    assert lb.get("source_session") == "sessAAAA"
    assert lb.get("event_ts") == "2026-07-09T10:00:00Z", "chunk ts carried VERBATIM"
    lb = _lb(base, OLD, "enriched:chunk456")
    assert lb.get("source_session") == "sessBBBB", "rotation's archive/ shards join too"
    assert lb.get("event_ts") == "2026-06-01T08:00:00Z"
    lb = _lb(base, OLD, "enriched:chunk789")
    assert lb.get("source_session") == "sessCCCC", "legacy <key>/mirror/ layout joins too"


def test_tier_b_recovers_the_event_date_from_the_record_id(pool):
    base = pool["base"]
    lb = _lb(base, OLD, "lesson:2026-07-01:0:deadbeef")
    assert lb.get("source_session") == "pre-migration", "session is unknowable for docs"
    assert lb.get("event_ts") == "2026-07-01", "bare date - the id's honest resolution"
    lb = _lb(base, OLD, "journal:undated:0:0badf00d")
    assert lb.get("source_session") == "pre-migration"
    assert "event_ts" not in lb, "an unrecoverable date is ABSENT, never fabricated"


def test_tier_c_sentinel_and_already_tagged_records_skipped(pool):
    base = pool["base"]
    lb = _lb(base, OLD, "working:pinned:cafebabecafebabe")
    assert lb.get("source_session") == "pre-migration" and "event_ts" not in lb
    lb = _lb(base, OLD, "journal:2026-07-05:0:c0ffee00")
    assert lb.get("source_session") == "eeee5555", "an existing tag is NEVER revised"
    assert lb.get("event_ts") == "2026-07-05T11:59:00Z"


def test_backfill_rerun_is_a_byte_identical_noop(pool):
    rc, out = pool["backfill2"]
    assert rc == 0, out
    assert "skipped 7" in out and "tier a 0" in out, out
    assert pool["snap_bf2"] == pool["snap_bf1"], "additive-only: a rerun rewrites nothing"


def test_typoed_weave_root_is_refused_before_any_write(pool):
    rc, out = pool["typo"]
    assert rc == 2 and "refusing" in out, \
        "a wrong sentinel is never revised, so a bad root must refuse up front"


# --- merge-scope -----------------------------------------------------------------

def test_merge_rehomes_relabels_and_preserves_provenance(pool):
    rc, out = pool["merge1"]
    assert rc == 0, out
    assert "stored 5 | duplicates 1 | retired 0 | conflicts 1 | ledger 7 | errors 0" in out, out
    lb = _lb(pool["base"], NEW, "enriched:chunk123")
    assert lb.get("scope_id") == NEW and lb.get("merged_from") == OLD
    assert lb.get("source_session") == "sessAAAA", "backfilled provenance rides along"
    assert lb.get("event_ts") == "2026-07-09T10:00:00Z"


def test_merge_preserves_the_created_label_verbatim(pool):
    lb = _lb(pool["base"], NEW, "journal:2026-07-05:0:c0ffee00")
    assert lb.get("created") == "2026-07-05T12:00Z", \
        "re-homing never re-enters the pool clock (recency must not see a fake 'new')"
    assert lb.get("source_session") == "eeee5555"


def test_merge_dedup_skips_loudly_and_leaves_the_destination_copy_untouched(pool):
    _, out = pool["merge1"]
    assert "already in %s (content-identical)" % NEW in out
    rec = _by_srid(pool["base"], NEW)["lesson:2026-07-01:0:deadbeef"]
    assert rec.id == pool["dup_id"], "content-derived ids agree across scopes"
    lb = dict(rec.labels or {})
    assert lb.get("scope_id") == NEW and "merged_from" not in lb


def test_merge_conflict_destination_version_wins(pool):
    _, out = pool["merge1"]
    assert "DIFFERENT content" in out, "the conflict skip is loud, never silent"
    rec = _by_srid(pool["base"], NEW)["journal:undated:0:0badf00d"]
    assert rec.id == pool["conf_id"], "the destination's version survives"
    assert "REVISED IN NEW" in rec.content


def test_merge_ledger_continuity_and_retirement_carried(pool):
    base = pool["base"]
    led = SyncLedger(base / NEW, NEW)
    for c in pool["cands"]:
        # BLOCKER 3: the ledger slot is scope_id + the pk (folds source_project in)
        key = "weave:%s:%s" % (NEW, c.provenance_key)
        e = led.entry(key)
        assert e is not None and e.get("state") == "active", key
    assert led.is_retired("weave:%s:journal:journal:2026-01-01:0:feedface" % NEW), \
        "retirement intent lives only in the ledger - it must carry"
    ctx = open_markdown(str(base / NEW))
    assert verify_ledger(ctx, RouterScope(scope_id=NEW), led) == []


def test_merge_conserves_every_old_file_and_never_touches_the_old_store(pool):
    lost = pool["before_files"] - pool["after_files"]
    assert not lost, "CONSERVATION VIOLATION: %r" % sorted(str(p) for p in lost)
    assert pool["old_store_after"] == pool["old_store_snap"], \
        "the old scope's records.jsonl is read-only to this tool"
    assert (pool["base"] / OLD / (".merged-into-%s" % NEW)).exists(), \
        "the marker is the audit breadcrumb AND the idempotency latch"


def test_merge_rerun_short_circuits_on_the_marker(pool):
    rc, out = pool["merge2"]
    assert rc == 0 and "already merged" in out, out
    assert pool["new_snap_m2"] == pool["new_snap_m1"], "rerun writes nothing to NEW"


def test_old_scope_ledger_and_records_stay_green_after_everything(pool):
    base = pool["base"]
    ctx = open_markdown(str(base / OLD))
    assert verify_ledger(ctx, RouterScope(scope_id=OLD),
                         SyncLedger(base / OLD, OLD)) == []
    assert len(_by_srid(base, OLD)) == 7, "every OLD record still lives in the OLD dir"
