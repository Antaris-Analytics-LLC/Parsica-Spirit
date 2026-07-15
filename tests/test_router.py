"""Router read-gateway: fail-CLOSED scope isolation for Spirit reads.

Two layers, mirroring the write-gateway enforcer pattern:
  * OUTCOME: the gateways refuse scope-less reads and surface cross-scope
    contamination loudly (IsolationViolation, never a silent drop).
  * STRUCTURAL: an AST scan of every parsica_spirit module, discovered recursively
    (not hand-listed), flags any ctx.query / ctx.all_records call site outside the
    allowlisted surfaces. A scanner self-test proves the scan has teeth. promote.py is
    deliberately NOT allowlisted: the moment it exists, a direct read goes red.
"""
import ast
import os
import tempfile

import pytest

from parsica_spirit import open_markdown
from parsica_spirit.router import (
    IsolationViolation,
    RouterScope,
    ScopeRequired,
    _spirit_query,
    _spirit_records,
    federated_hits,
    layer_order,
    strip_provenance_tag,
)


def _ctx():
    return open_markdown(tempfile.mkdtemp(prefix="rt_store_"))


A = RouterScope(scope_id="proj-a-0011223344")
B = RouterScope(scope_id="proj-b-5566778899")


def _seed(ctx, body, scope_id=None):
    labels = {"scope_id": scope_id} if scope_id else None
    r = ctx.store(body, labels=labels, enrich=False)
    assert r.stored
    return r.id


# --- scope is REQUIRED (fail closed, before any read runs) ---

def test_query_requires_scope():
    ctx = _ctx()
    for bad in (RouterScope(scope_id=""), RouterScope(scope_id="   "), RouterScope(scope_id=None)):
        with pytest.raises(ScopeRequired):
            _spirit_query(ctx, bad, "anything")
        with pytest.raises(ScopeRequired):
            _spirit_records(ctx, bad)


def test_extra_labels_cannot_smuggle_a_different_scope():
    ctx = _ctx()
    _seed(ctx, "alpha fact about widgets", scope_id=A.scope_id)
    with pytest.raises(IsolationViolation):
        _spirit_query(ctx, A, "widgets", labels={"scope_id": B.scope_id})


# --- per-project mode: unlabeled = in-scope, foreign = LOUD ---

def test_per_project_query_returns_scoped_and_unlabeled():
    ctx = _ctx()
    _seed(ctx, "alpha fact about widgets", scope_id=A.scope_id)
    _seed(ctx, "plain fact about widgets")          # pre-router record, no scope label
    hits = _spirit_query(ctx, A, "widgets")
    bodies = " ".join(r.content for r in hits)
    assert "alpha fact" in bodies and "plain fact" in bodies


def test_per_project_foreign_scope_is_contamination_not_silence():
    ctx = _ctx()
    _seed(ctx, "alpha fact about widgets", scope_id=A.scope_id)
    _seed(ctx, "bravo fact about widgets", scope_id=B.scope_id)
    with pytest.raises(IsolationViolation):
        _spirit_query(ctx, A, "widgets")
    with pytest.raises(IsolationViolation):
        _spirit_records(ctx, A)


def test_per_project_records_returns_scoped_and_unlabeled():
    ctx = _ctx()
    _seed(ctx, "alpha fact about widgets", scope_id=A.scope_id)
    _seed(ctx, "plain fact about widgets")
    recs = _spirit_records(ctx, A)
    assert len(recs) == 2


# --- federation entry (the surface Weave's query.py lazy-imports) -----------

def test_federated_hits_missing_root_returns_empty_and_never_scaffolds():
    root = os.path.join(tempfile.mkdtemp(prefix="rt_fed_"), "does-not-exist")
    assert federated_hits(root, A.scope_id, "anything") == []
    assert not os.path.exists(root), "a READ must never create the store"


def test_federated_hits_strips_tag_and_carries_labels():
    root = tempfile.mkdtemp(prefix="rt_fedhit_")
    ctx = open_markdown(root)
    body = "[weave aabbccdd · lesson · lesson:2026-07-01:0:11223344 · src:55667788]\n\n" \
           "the gateway lesson body"
    ctx.store(body, labels={"scope_id": A.scope_id, "source_kind": "lesson",
                            "source_record_id": "lesson:2026-07-01:0:11223344",
                            "content_hash": "cafe1234cafe1234"}, enrich=False)
    hits = federated_hits(root, A.scope_id, "gateway lesson")
    assert len(hits) == 1
    h = hits[0]
    assert h["body"] == "the gateway lesson body", "provenance tag stripped for display"
    assert h["kind"] == "lesson" and h["source_record_id"].startswith("lesson:")
    assert h["content_hash"] == "cafe1234cafe1234"


def test_strip_provenance_tag_is_conservative():
    assert strip_provenance_tag("[weave x · y]\n\nbody") == "body"
    assert strip_provenance_tag("[claude project · T]\n\nbody") == "body"
    assert strip_provenance_tag("plain body") == "plain body"
    assert strip_provenance_tag("[NOTE] not a tag\n\nkeep me") == "[NOTE] not a tag\n\nkeep me"


def test_layer_order_nudge_is_deterministic_and_defaults_weave_first():
    assert layer_order("what did we just do") == ("weave", "spirit")
    assert layer_order("what do you remember about the fence") == ("spirit", "weave")
    assert layer_order("plain factual question") == ("weave", "spirit")
    # both buckets match -> default, the nudge never fights itself
    assert layer_order("recent standing decision") == ("weave", "spirit")


# --- STRUCTURAL: the two gateways are the only door -------------------------

_ALLOWED_READ_SURFACES = {
    "router.py": "the gateway itself - the sole door",
    "context.py": "the facade that DEFINES query/all_records and delegates to its backend",
    "claude_memory.py": "import reconciliation over its own target store (pre-router, reviewed)",
}
# backends IMPLEMENT the port; engine + enrich are the OTHER zones of the one package
# (the engine's internal reads and the leaf's conformance suite are not router-subsystem
# Spirit reads - the engine backend is itself a StoragePort implementation, and the zone
# boundaries have their own enforcement in test_seam_guards.py / .importlinter).
_SKIP_DIRS = {"backends", "engine", "enrich", "tests", "__pycache__"}


def _read_ops_in_source(src):
    """Every ATTRIBUTE mention of .query / .all_records (calls AND aliases - `m =
    ctx.query` is a read the moment it exists) plus getattr(x, 'query'/'all_records').
    Conservative by design - a false positive earns an allowlist entry with a written
    reason. Known residual: __getattribute__/string-dispatch dodges; the filesystem
    boundary is the outcome-level backstop in per-project mode."""
    hits = []
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Attribute) and n.attr in ("query", "all_records"):
            hits.append("%d:.%s" % (n.lineno, n.attr))
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "getattr":
            for a in n.args:
                if isinstance(a, ast.Constant) and a.value in ("query", "all_records"):
                    hits.append("%d:getattr(%s)" % (n.lineno, a.value))
    return hits


def test_scanner_selftest():
    assert _read_ops_in_source("ctx.query('x', labels=l)")
    assert _read_ops_in_source("ctx.all_records()")
    assert _read_ops_in_source("self._backend.query(t, limit=5)")
    assert _read_ops_in_source("m = ctx.query\nm('y')"), "aliasing is a read"
    assert _read_ops_in_source("getattr(ctx, 'query')('x')"), "getattr dodge is caught"
    assert not _read_ops_in_source(
        "query(x)\nrecords = all_records\nx.record()\nd = {'k': 'query'}")


_WALL_CLOCK = ("datetime.now", "time.time", "getmtime", "random.random", "random.choice",
               "datetime.utcnow", "time.monotonic")
#: the ranking path BY DESIGN: the gateway/merge module and the planner. trace.py is
#: deliberately excluded - an observational timestamp on a log line is not a ranking
#: input (its docstring says the same).
_RANKING_MODULES = ("router.py", "promote.py")


def test_no_wall_clock_in_the_ranking_path():
    import parsica_spirit
    pkg = os.path.dirname(os.path.abspath(parsica_spirit.__file__))
    offenders = []
    for fn in _RANKING_MODULES:
        src = open(os.path.join(pkg, fn), encoding="utf-8").read()
        for marker in _WALL_CLOCK:
            if marker in src:
                offenders.append("%s: %s" % (fn, marker))
    assert not offenders, "wall-clock/randomness in the ranking path: %r" % offenders


def test_no_direct_spirit_reads_outside_gateways():
    import parsica_spirit
    pkg = os.path.dirname(os.path.abspath(parsica_spirit.__file__))
    offenders = []
    for root, dirs, files in os.walk(pkg):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fn in files:
            if not fn.endswith(".py") or fn in _ALLOWED_READ_SURFACES:
                continue
            src = open(os.path.join(root, fn), encoding="utf-8").read()
            ops = _read_ops_in_source(src)
            if ops:
                offenders.append("%s: %s" % (os.path.relpath(os.path.join(root, fn), pkg), ops))
    assert not offenders, "direct Spirit reads outside the gateways: %r" % offenders
