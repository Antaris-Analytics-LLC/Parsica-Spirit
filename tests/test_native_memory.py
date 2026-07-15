"""Native-memory permanence class: the host agent's own memory files, imported
read-only, rank boosted-but-beatable and identify themselves at retrieval.

  * READ-ONLY IMPORT: importing never writes the source files - byte-for-byte proof,
    first import and re-import alike (the uninstall guarantee is this same property:
    nothing Spirit does ever mutates CLAUDE.md / MEMORY.md / topic files).
  * WEIGHT PIN: NATIVE_MEMORY_WEIGHT is the fixed constant 2, defined exactly once -
    the operator-visible module constant is its only writer; no code path assigns it.
  * BOOSTED-BUT-BEATABLE: at equal overlap the native record wins; a session record
    with more than WEIGHT x the overlap still wins (a dead-on session hit must be
    able to beat a stale note).
  * PURITY: ranked order is identical across repeated queries with native records
    present (the conformance contract the weight must not break).
  * FEDERATION: imported records pass the scope gate (the unlabeled pre-router
    admission built for them) and _hit_dict projects their provenance.
"""
import os
import re
from pathlib import Path

import parsica_spirit
from parsica_spirit import open_markdown
from parsica_spirit.backends._common import (NATIVE_MEMORY_SOURCE, NATIVE_MEMORY_WEIGHT,
                                             record_weight)
from parsica_spirit.backends.memory import InMemoryBackend
from parsica_spirit.claude_memory import import_claude_memory
from parsica_spirit.router import federated_hits


def _seed_native_files(root: Path):
    proj = root / "proj"
    mem = root / "mem"
    proj.mkdir()
    mem.mkdir()
    (proj / "CLAUDE.md").write_text(
        "# Rules\n\n## Deploy\nAlways run the smoke suite before deploying anywhere.\n",
        encoding="utf-8")
    (mem / "MEMORY.md").write_text(
        "# Memory Index\n\n- [Retry policy](retry.md) - exponential backoff everywhere\n",
        encoding="utf-8")
    (mem / "retry.md").write_text(
        "# Retry policy\n\nRetries use exponential backoff with jitter, capped at five.\n",
        encoding="utf-8")
    return proj, mem


def _snapshot(paths):
    out = {}
    for p in paths:
        for f in sorted(Path(p).rglob("*")):
            if f.is_file():
                st = f.stat()
                out[str(f)] = (f.read_bytes(), st.st_mtime_ns)
    return out


def test_import_leaves_native_files_byte_for_byte(tmp_path):
    proj, mem = _seed_native_files(tmp_path)
    before = _snapshot([proj, mem])
    ctx = open_markdown(str(tmp_path / "pool"))
    r1 = import_claude_memory(ctx, project_root=str(proj), memory_dir=str(mem))
    assert r1.added > 0
    assert _snapshot([proj, mem]) == before, "first import touched a native file"
    r2 = import_claude_memory(ctx, project_root=str(proj), memory_dir=str(mem))
    assert r2.added == 0 and r2.updated == 0 and r2.unchanged == r1.added
    assert _snapshot([proj, mem]) == before, "re-import touched a native file"


def test_native_weight_is_pinned_and_never_assigned():
    assert NATIVE_MEMORY_WEIGHT == 2
    assert record_weight({"source": NATIVE_MEMORY_SOURCE}) == 2
    assert record_weight({"source": "anything-else"}) == 1
    assert record_weight({}) == 1 and record_weight(None) == 1
    # the fixed-constant law: the definition in _common.py is the ONLY assignment in
    # the package - no code path (import, query, calibration, anything) adjusts it.
    pkg = Path(parsica_spirit.__file__).parent
    assign = re.compile(r"NATIVE_MEMORY_WEIGHT\s*=")
    sites = []
    for f in pkg.rglob("*.py"):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if assign.search(line):
                sites.append("%s:%d" % (f.name, i))
    assert sites == ["_common.py:%s" % sites[0].split(":")[1]] and len(sites) == 1, sites


def _seed_ranked(store_fn):
    """Three records against the query 'alpha beta gamma delta epsilon': a native note
    and a session note with EQUAL overlap (2), and a session note with overlap 5 -
    more than WEIGHT x the native's 2."""
    store_fn("alpha beta stale native note", {"source": NATIVE_MEMORY_SOURCE})
    store_fn("alpha beta ordinary session note", {"source": "session"})
    store_fn("alpha beta gamma delta epsilon dead-on session hit", {"source": "session"})


def test_boosted_but_beatable_in_memory_backend():
    be = InMemoryBackend()
    _seed_ranked(lambda c, lb: be.store(c, labels=lb))
    got = [r.content for r in be.query("alpha beta gamma delta epsilon")]
    assert got[0].startswith("alpha beta gamma delta epsilon"), "a dead-on hit must win"
    assert "native" in got[1], "at equal overlap the native class outranks the session note"
    assert "ordinary" in got[2]


def test_boosted_but_beatable_markdown_backend(tmp_path):
    ctx = open_markdown(str(tmp_path / "pool"))
    _seed_ranked(lambda c, lb: ctx.store(c, labels=lb))
    got = [r.content for r in ctx.query("alpha beta gamma delta epsilon")]
    assert got[0].startswith("alpha beta gamma delta epsilon")
    assert "native" in got[1]
    assert "ordinary" in got[2]


def test_query_purity_with_native_records(tmp_path):
    ctx = open_markdown(str(tmp_path / "pool"))
    _seed_ranked(lambda c, lb: ctx.store(c, labels=lb))
    first = [r.id for r in ctx.query("alpha beta gamma delta epsilon")]
    for _ in range(3):
        assert [r.id for r in ctx.query("alpha beta gamma delta epsilon")] == first


def test_federation_surfaces_native_with_provenance(tmp_path):
    proj, mem = _seed_native_files(tmp_path)
    pool = tmp_path / "pool"
    ctx = open_markdown(str(pool))
    import_claude_memory(ctx, project_root=str(proj), memory_dir=str(mem))
    hits = federated_hits(str(pool), "scope-xyz", "exponential backoff jitter")
    assert hits, "imported native records must pass the scope gate (unlabeled admission)"
    top = hits[0]
    assert top["source"] == NATIVE_MEMORY_SOURCE
    assert top["source_scope"] in ("auto", "project", "local", "user")
    assert top["heading_path"]
    assert not top["body"].startswith("[claude"), "provenance tag is stripped for display"
    assert top["content_hash"]
