"""The markdown backend must pass the SAME conformance suite as the fake, plus durability."""

import os

import pytest

from parsica_spirit.backends.markdown import MarkdownBackend
from parsica_spirit.conformance import run_storage_conformance
from parsica_spirit.enrich import EnrichmentRecord


def _snapshot(root):
    """Every file under root -> (size, mtime_ns, content). The exact footprint a read must
    not disturb: a new path (e.g. .parsica_md.lock), a changed size/mtime, or altered bytes
    all show up as a diff."""
    snap = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = os.path.join(dirpath, name)
            st = os.stat(p)
            with open(p, "rb") as fh:
                snap[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns, fh.read())
    return snap


def test_markdown_backend_is_conformant(tmp_path):
    run_storage_conformance(lambda: MarkdownBackend(str(tmp_path)))


def test_markdown_persists_across_instances(tmp_path):
    root = str(tmp_path)
    b1 = MarkdownBackend(root)
    rid = b1.store("survives a reboot", labels={"k": "v"},
                   enrichment=EnrichmentRecord(keywords=("durability",)))
    # a brand-new backend instance on the same root = a "next day boot"
    b2 = MarkdownBackend(root)
    got = b2.get(rid)
    assert got is not None
    assert got.content == "survives a reboot"
    assert got.labels["k"] == "v"
    assert got.enrichment is not None and "durability" in got.enrichment.keywords
    assert any(r.id == rid for r in b2.query("durability"))  # queryable via persisted enrichment


def test_markdown_recycle_survives_restart(tmp_path):
    root = str(tmp_path)
    b1 = MarkdownBackend(root)
    rid = b1.store("deleted but recoverable")
    assert b1.forget(rid) is True
    b2 = MarkdownBackend(root)
    assert b2.get(rid) is None
    assert any(r.id == rid for r in b2.recycle_bin())  # tombstone survived the restart
    assert b2.restore(rid) is True
    assert b2.get(rid) is not None


# --- read-only mode (B3: zero-footprint federated / isolated reads) ---

def test_read_only_open_requires_existing_dir(tmp_path):
    """A read never scaffolds: opening read-only on a path that does not exist raises rather
    than creating the store dir."""
    missing = str(tmp_path / "does_not_exist")
    with pytest.raises(FileNotFoundError):
        MarkdownBackend(missing, read_only=True)


def test_read_only_read_leaves_zero_footprint(tmp_path):
    """THE B3 property. A federated/isolated read opens the store read-only; every read path
    (get / query / all_records / recycle_bin) must leave the store byte-identical - crucially it
    must NOT create the .parsica_md.lock, which the normal refresh-under-lock opens with O_CREAT.

    The seed WRITE legitimately creates the lock (a write has a footprint); we clear it to model a
    store the isolated read opens fresh, then prove the read never brings it back."""
    root = str(tmp_path)
    seed = MarkdownBackend(root)                       # normal open populates the store
    rid = seed.store("a federated read target", labels={"scope_id": "s1"},
                     enrichment=EnrichmentRecord(keywords=("federation",)))
    os.remove(os.path.join(root, ".parsica_md.lock"))  # clean baseline: the read must not recreate it

    before = _snapshot(root)
    assert ".parsica_md.lock" not in before            # baseline has no lock
    ro = MarkdownBackend(root, read_only=True)
    assert ro.get(rid) is not None
    assert any(r.id == rid for r in ro.query("federation"))
    assert any(r.id == rid for r in ro.all_records())
    _ = ro.recycle_bin()
    # a second read-only instance (the "long-lived instance refreshes" path) still no-locks
    MarkdownBackend(root, read_only=True).query("federation")

    after = _snapshot(root)
    assert after == before, (
        "a read-only read changed the store footprint: added=%r removed=%r"
        % (sorted(set(after) - set(before)), sorted(set(before) - set(after))))
    assert ".parsica_md.lock" not in after             # the read never created the lock


def test_read_only_refuses_every_mutation(tmp_path):
    """Read-only is enforced at the seam: every durable mutation raises PermissionError, and the
    refusal itself writes nothing (no lock, no records mutation)."""
    root = str(tmp_path)
    seed = MarkdownBackend(root)
    rid = seed.store("immutable under a read-only view")
    os.remove(os.path.join(root, ".parsica_md.lock"))  # clean baseline (the seed write made a lock)
    before = _snapshot(root)
    assert ".parsica_md.lock" not in before

    ro = MarkdownBackend(root, read_only=True)
    with pytest.raises(PermissionError):
        ro.store("nope")
    with pytest.raises(PermissionError):
        ro.forget(rid)
    with pytest.raises(PermissionError):
        ro.modify(rid, labels={"x": "y"})
    with pytest.raises(PermissionError):
        ro.move(rid, labels={"x": "y"})
    with pytest.raises(PermissionError):
        ro.restore(rid)
    with pytest.raises(PermissionError):
        ro.plan_reset(scope="all", disposition="wipe")
    with pytest.raises(PermissionError):
        ro.execute_reset("deadbeef")

    assert _snapshot(root) == before          # not one byte moved, and no lock file appeared
    assert ".parsica_md.lock" not in _snapshot(root)
