"""EngineBackend durability: everything survives a fresh backend instance on the same workspace.

The seam's whole promise is persistence; same-instance conformance cannot catch a store that
holds records only in memory. Every test here constructs a SECOND (and third) EngineBackend on
the same workspace and asserts the state a caller stored is still there.
"""
import tempfile

from parsica_spirit.enrich import EnrichmentRecord

from parsica_spirit.engine.seam import EngineBackend


def _ws():
    return tempfile.mkdtemp(prefix="pcx_seamdur_")


def test_content_labels_enrichment_survive_reopen():
    ws = _ws()
    b1 = EngineBackend(ws)
    rid = b1.store("retry cap is 60 seconds", labels={"topic": "ops"},
                   enrichment=EnrichmentRecord(summary="ops retry policy", keywords=("retry", "cap")))
    b2 = EngineBackend(ws)
    got = b2.get(rid)
    assert got is not None
    assert got.content == "retry cap is 60 seconds"
    assert got.labels["topic"] == "ops"
    assert got.enrichment is not None and got.enrichment.summary == "ops retry policy"
    assert [r.id for r in b2.query("retry cap")], "query must find the record after reopen"


def test_forget_and_recycle_bin_survive_reopen():
    ws = _ws()
    b1 = EngineBackend(ws)
    rid = b1.store("to be forgotten content", labels={"k": "v"})
    assert b1.forget(rid)
    b2 = EngineBackend(ws)
    assert b2.get(rid) is None, "forgotten record must stay forgotten after reopen"
    bin_recs = {r.id: r for r in b2.recycle_bin()}
    assert rid in bin_recs, "recycle bin must survive reopen"
    assert bin_recs[rid].labels == {"k": "v"}, "bin snapshot labels must survive reopen"


def test_restore_after_reopen_and_third_open():
    ws = _ws()
    b1 = EngineBackend(ws)
    rid = b1.store("restorable content here", labels={"a": "b"})
    assert b1.forget(rid)
    b2 = EngineBackend(ws)
    assert b2.restore(rid), "restore must work on a reopened backend"
    b3 = EngineBackend(ws)
    got = b3.get(rid)
    assert got is not None and got.labels == {"a": "b"}, "restored state must survive the NEXT reopen"


def test_wipe_survives_reopen():
    ws = _ws()
    b1 = EngineBackend(ws)
    r1 = b1.store("alpha content one")
    b1.store("beta content two")
    plan = b1.plan_reset(scope="all", disposition="wipe")
    assert b1.execute_reset(plan.plan_hash) == 2
    b2 = EngineBackend(ws)
    assert b2.all_records() == [], "wiped store must stay empty after reopen"
    assert {r.id for r in b2.recycle_bin()} >= {r1}, "wipe bin must survive reopen"


def test_modify_and_move_survive_reopen():
    ws = _ws()
    b1 = EngineBackend(ws)
    rid = b1.store("modifiable content", labels={"stage": "draft"})
    assert b1.modify(rid, labels={"stage": "final"},
                     enrichment=EnrichmentRecord(summary="finalized"))
    assert b1.move(rid, labels={"shelf": "archive"})
    b2 = EngineBackend(ws)
    got = b2.get(rid)
    assert got.labels["stage"] == "final"
    assert got.labels["shelf"] == "archive"
    assert got.enrichment is not None and got.enrichment.summary == "finalized"


def test_fresh_workspace_opens_empty():
    b = EngineBackend(_ws())
    assert b.all_records() == []
    assert b.recycle_bin() == []


def test_concurrent_process_writes_survive():
    """Concurrent-write repro: N processes each store a unique record on the SAME workspace;
    a fresh backend must see all N (no last-writer-wins snapshot clobbering)."""
    import os
    import pathlib
    import subprocess
    import sys

    ws = _ws()
    root = pathlib.Path(__file__).resolve().parents[2]
    src = str(root / "src")
    env = {**os.environ, "PYTHONPATH": src + os.pathsep + os.environ.get("PYTHONPATH", "")}
    code = ("import sys\n"
            "from parsica_spirit.engine.seam import EngineBackend\n"
            "EngineBackend(sys.argv[1]).store('unique record %s' % sys.argv[2])\n")
    procs = [subprocess.Popen([sys.executable, "-c", code, ws, str(i)], env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for i in range(8)]
    for pr in procs:
        _, err = pr.communicate(timeout=120)
        assert pr.returncode == 0, err.decode("utf-8", "replace")
    from parsica_spirit.engine.seam import EngineBackend
    got = {r.content for r in EngineBackend(ws).all_records()}
    want = {"unique record %d" % i for i in range(8)}
    missing = want - got
    assert not missing, "lost concurrent writes: %r" % missing


# --- Regression: the sidecar fails CLOSED, never open --------

def test_corrupt_sidecar_fails_closed_and_bak_recovers():
    """Regression: corrupt .parsica_seam.json used to load as EMPTY side maps, and the
    next mutation persisted the loss (labels incl. scope isolation, enrichment, bin).
    Now: an unreadable EXISTING sidecar refuses to open, and the .bak kept by _persist
    restores the full metadata."""
    import os
    import pytest
    from parsica_spirit.engine.seam import SeamSidecarError

    ws = _ws()
    b1 = EngineBackend(ws)
    rid = b1.store("scoped durable fact", labels={"scope_id": "abc", "topic": "ops"},
                   enrichment=EnrichmentRecord(summary="retry policy"))
    b1.store("second write refreshes the .bak", labels={"scope_id": "abc"})
    side = os.path.join(ws, EngineBackend.SIDECAR)
    assert os.path.exists(side + ".bak"), "_persist keeps the previous good sidecar"

    with open(side, "w", encoding="utf-8") as f:
        f.write("{ this is not json")
    with pytest.raises(SeamSidecarError):
        EngineBackend(ws)                      # fail CLOSED: no silent empty maps
    # malformed-but-parseable (not an object) also refuses
    with open(side, "w", encoding="utf-8") as f:
        f.write("[]")
    with pytest.raises(SeamSidecarError):
        EngineBackend(ws)

    # manual repair per the error message: restore the .bak -> full metadata is back
    os.replace(side + ".bak", side)
    b2 = EngineBackend(ws)
    got = b2.get(rid)
    assert got is not None and got.labels.get("scope_id") == "abc"
    assert got.enrichment is not None and got.enrichment.summary == "retry policy"


def test_missing_sidecar_still_means_fresh_workspace():
    """A MISSING sidecar is not corruption: a brand-new workspace opens clean."""
    ws = _ws()
    b = EngineBackend(ws)
    assert b.all_records() == []
