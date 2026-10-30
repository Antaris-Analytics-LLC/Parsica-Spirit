"""Fail-closed gates: REVIEW-2026-10.md, pile one, class (a).

Each test names the lines it closes at a1a2349 and proves both directions: BLOCKED (the gate
refuses and the write does not happen) and ALLOWED (ordinary input still passes).

  1. enrich/scrub.py 690-691: an enrichment that cannot be rebuilt with its scrubbed fields raises
     ScrubGateError instead of passing through unscrubbed; a real record is still scrubbed.
  2. enrich/scrub.py 704-707: label keys are scrubbed, numeric values that redact land as the
     marker, container values and non-string keys are refused, scalars keep their types, and two
     keys that collide after scrubbing are refused. Both shipped backends refuse the whole write
     and leave the store unchanged.
  3. identity.py 343-344: an identity with no fingerprint is not usable: Context.identity is None,
     the byline is empty, the banner shouts, promotion is blocked, and rescope_after_move binds a
     fresh identity while keeping the old file aside.
  4. engine/seam.py 97-100 and 273-274: a store whose entries cannot be loaded is refused at open,
     and a mutation whose reload fails does not run and persists nothing.
  5. engine/core.py 3606-3607 and 3683-3684: a bootstrap check that raises is reported as not OK,
     a file that cannot be inspected is reported, and an ordinary store still reports OK.
"""
import json
import os
import tempfile

import pytest

from parsica_spirit import open_markdown
from parsica_spirit.backends.markdown import MarkdownBackend
from parsica_spirit.enrich import EnrichmentRecord
from parsica_spirit.enrich.scrub import ScrubGateError, scrub_write
from parsica_spirit.engine import MemorySystem
from parsica_spirit.engine.seam import EngineBackend, SeamEngineLoadError
from parsica_spirit.identity import (IdentityStatus, load_identity_status, render_byline_directive,
                                     render_identity_banner, rescope_after_move, workspace_fingerprint)
from parsica_spirit.promote import PromotionBlocked, SyncLedger, apply_promotion

SECRET = "sk-ant-api03-" + "A" * 32
PII = "415-555-1212"
CARD = 4111111111111111          # a Luhn-valid test card number, as an int


class NotADataclass:
    """Has every enrichment field but is not a dataclass, so dataclasses.replace cannot rebuild it."""
    summary = "summary " + SECRET
    tags = ()
    keywords = ()
    search_queries = ()

    def is_empty(self):
        return False


# ---------------------------------------------------------------- 1. the enrichment scrub

def test_enrichment_that_cannot_be_scrubbed_refuses_the_write():
    with pytest.raises(ScrubGateError):
        scrub_write("body", {}, NotADataclass())
    # ALLOWED: a real record is scrubbed, not refused
    _, _, enr = scrub_write("body", {}, EnrichmentRecord(summary="s " + SECRET, keywords=(PII,)))
    assert SECRET not in enr.summary and PII not in enr.keywords


@pytest.mark.parametrize("make", [
    lambda root: EngineBackend(root, agent_name="t"),
    lambda root: MarkdownBackend(root),
], ids=["engine", "markdown"])
def test_backends_refuse_the_whole_write_when_the_gate_cannot_scrub(tmp_path, make):
    b = make(str(tmp_path))
    rid = b.store("an ordinary durable note about the release plan")
    before = sorted(r.id for r in b.all_records())
    with pytest.raises(ScrubGateError):
        b.store("a second note", enrichment=NotADataclass())
    with pytest.raises(ScrubGateError):
        b.modify(rid, labels={"k": ["a", "list"]})
    assert sorted(r.id for r in b.all_records()) == before, "a refused write must leave the store unchanged"
    blob = ""
    for dirpath, _, files in os.walk(str(tmp_path)):
        for f in files:
            blob += open(os.path.join(dirpath, f), encoding="utf-8", errors="ignore").read()
    assert SECRET not in blob and "a second note" not in blob


# ---------------------------------------------------------------- 2. label keys and values

def test_label_keys_and_numbers_are_scrubbed_and_containers_refused():
    _, labels, _ = scrub_write(None, {"key " + SECRET: "v", "phone": PII, "card": CARD})
    assert all(SECRET not in k for k in labels), labels
    assert PII not in json.dumps(labels)
    assert labels["card"] != CARD and "CARD" in str(labels["card"]), labels
    # ALLOWED: plain scalars keep their types and values
    _, labels, _ = scrub_write(None, {"a": "x", "b": 3, "c": 1.5, "d": True, "e": None})
    assert labels == {"a": "x", "b": 3, "c": 1.5, "d": True, "e": None}
    # BLOCKED: containers and non-string keys cannot be scrubbed field by field
    with pytest.raises(ScrubGateError):
        scrub_write(None, {"k": ["a", "list"]})
    with pytest.raises(ScrubGateError):
        scrub_write(None, {"k": {"nested": "dict"}})
    with pytest.raises(ScrubGateError):
        scrub_write(None, {("tuple", "key"): "v"})
    # BLOCKED: two keys that collide after scrubbing with different values
    with pytest.raises(ScrubGateError):
        scrub_write(None, {"k " + SECRET: "one", "k " + "sk-ant-api03-" + "B" * 32: "two"})


# ---------------------------------------------------------------- 3. identity without a fingerprint

def _plant_unbound_identity(root, name="Elder"):
    doc = {"identity_id": "a" * 32, "display_name": name, "workspace_fingerprint": "",
           "provenance": "self", "created": "2020-01-01T00:00:00+00:00", "schema_version": 1}
    with open(os.path.join(root, "identity.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f)


def test_identity_without_fingerprint_is_not_usable_and_blocks_promotion():
    root = tempfile.mkdtemp(prefix="fc_ident_")
    _plant_unbound_identity(root)
    view = load_identity_status(root)
    assert view.status == IdentityStatus.LEGACY and not view.usable
    assert "NONE" in render_identity_banner(root)
    assert render_byline_directive(root, session_tag="sess1234") == ""
    ctx = open_markdown(root)
    assert ctx.identity is None, "the facade withholds an unbound identity"
    with pytest.raises(PromotionBlocked):
        apply_promotion(ctx, [], SyncLedger(root, "scope-x"))
    # ALLOWED: the recovery path binds a fresh identity and keeps the old file aside
    fresh = rescope_after_move(root, display_name="Elder")
    assert fresh.workspace_fingerprint == workspace_fingerprint(root)
    assert load_identity_status(root).usable and ctx.identity is not None
    assert any(n.startswith("identity.legacy.") for n in os.listdir(os.path.join(root, ".identity_quarantine")))


def test_blanking_the_fingerprint_on_a_copy_no_longer_passes_copy_detection():
    a = tempfile.mkdtemp(prefix="fc_copy_a_")
    ctx_a = open_markdown(a)
    ctx_a.scope_identity("Original")
    b = tempfile.mkdtemp(prefix="fc_copy_b_")
    with open(os.path.join(a, "identity.json"), encoding="utf-8") as f:
        doc = json.load(f)
    doc["workspace_fingerprint"] = ""                       # the one-field edit that used to pass
    with open(os.path.join(b, "identity.json"), "w", encoding="utf-8") as f:
        json.dump(doc, f)
    assert open_markdown(b).identity is None, "a copy with the fingerprint blanked must not be usable"
    assert open_markdown(b).byline_directive(session_tag="abc") == ""
    assert open_markdown(a).identity is not None, "the original is still bound and usable"


# ---------------------------------------------------------------- 4. the seam's engine load

def _boom(self, *args, **kwargs):
    raise ValueError("a shard file that does not parse")


def test_engine_backend_refuses_to_open_when_the_store_cannot_be_loaded(monkeypatch):
    root = tempfile.mkdtemp(prefix="fc_seam_open_")
    monkeypatch.setattr(MemorySystem, "load", _boom)
    with pytest.raises(SeamEngineLoadError):
        EngineBackend(root, agent_name="t")


def test_engine_backend_refuses_a_mutation_when_the_reload_fails(monkeypatch):
    root = tempfile.mkdtemp(prefix="fc_seam_reload_")
    b = EngineBackend(root, agent_name="t")
    b.store("a durable release note about the plan for the week")
    before = sorted(r.id for r in b.all_records())
    monkeypatch.setattr(MemorySystem, "load", _boom)
    with pytest.raises(SeamEngineLoadError):
        b.store("another note that must not land")
    monkeypatch.undo()
    reopened = EngineBackend(root, agent_name="t")      # ALLOWED again once the store loads
    assert sorted(r.id for r in reopened.all_records()) == before
    assert reopened.store("an ordinary note after the repair")


# ---------------------------------------------------------------- 5. the health check

def test_health_marks_a_failed_bootstrap_check_as_not_ok(monkeypatch):
    m = MemorySystem(tempfile.mkdtemp(prefix="fc_health_"), agent_name="t")
    ok = m.get_health()
    assert ok["checks"]["bootstrap_files_ok"] is True and "bootstrap_check_error" not in ok   # ALLOWED
    monkeypatch.setattr(MemorySystem, "check_bootstrap_files", _boom)
    degraded = m.get_health()
    assert degraded["checks"]["bootstrap_files_ok"] is False
    assert degraded["status"] == "degraded" and degraded["bootstrap_check_error"] == "ValueError"


def test_check_bootstrap_files_reports_a_file_it_cannot_inspect(tmp_path, monkeypatch):
    (tmp_path / "MEMORY.md").write_text("ordinary bootstrap text", encoding="utf-8")
    m = MemorySystem(str(tmp_path / "store"), agent_name="t")
    assert m.check_bootstrap_files(workspace_dir=str(tmp_path)) == []                       # ALLOWED
    real = os.path.getsize

    def flaky(p):
        if str(p).endswith("MEMORY.md"):
            raise OSError("stat failed")
        return real(p)

    monkeypatch.setattr(os.path, "getsize", flaky)
    out = m.check_bootstrap_files(workspace_dir=str(tmp_path))
    assert len(out) == 1 and "could not be inspected" in out[0] and "MEMORY.md" in out[0]
