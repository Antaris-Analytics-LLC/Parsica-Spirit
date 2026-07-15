"""The scrub gate is enforced AT THE SEAM: no write path lands a secret/PII on disk.

Probes every StoragePort mutation (store / modify / move) on both shipped backends, plus the
engine's own ingest, with a secret and PII placed in content, in a label value, and in an
enrichment field - then asserts the raw secret/PII never reaches ANY store file, including the
`.parsica_seam.json` sidecar dotfile. This is the regression guard for the "no way around it"
guarantee: the scrub lives in the shared leaf (`parsica_spirit.enrich.scrub.scrub_write`) and every
seam write crosses it.
"""
import os
import tempfile

import pytest

from parsica_spirit.enrich import EnrichmentRecord
from parsica_spirit.engine import MemorySystem
from parsica_spirit.engine.seam import EngineBackend
from parsica_spirit.backends.markdown import MarkdownBackend

SECRET = "sk-ant-api03-" + "A" * 32
PII = "415-555-1212"
ENR = EnrichmentRecord(summary=f"summary {SECRET}", keywords=(PII,),
                       search_queries=(SECRET,), tags=(PII,))


def _disk(root):
    blob = ""
    for dirpath, _, files in os.walk(root):   # os.walk INCLUDES dotfiles (the seam sidecar)
        for f in files:
            try:
                blob += open(os.path.join(dirpath, f), encoding="utf-8", errors="ignore").read()
            except OSError:
                pass
    return blob


def _clean(blob, where):
    assert SECRET not in str(blob), f"raw secret reached {where}"
    assert PII not in str(blob), f"raw PII reached {where}"


@pytest.mark.parametrize("make", [
    lambda root: EngineBackend(root, agent_name="t"),
    lambda root: MarkdownBackend(root),
], ids=["engine", "markdown"])
def test_no_seam_write_path_lands_a_secret_on_disk(tmp_path, make):
    root = str(tmp_path)
    b = make(root)
    rid = b.store(f"body {SECRET} phone {PII} about the durable release run today",
                  labels={"k": SECRET}, enrichment=ENR)
    r = b.get(rid)
    _clean(r.content, "store content")
    _clean(r.labels, "store label")
    if r.enrichment is not None:
        _clean((r.enrichment.summary, r.enrichment.keywords,
                r.enrichment.search_queries, r.enrichment.tags), "store enrichment")
    b.modify(rid, labels={"m": PII}, enrichment=ENR)
    _clean(b.get(rid).labels, "modify label")
    b.move(rid, labels={"v": SECRET})
    _clean(b.get(rid).labels, "move label")
    _clean(_disk(root), "disk (incl sidecar dotfile)")


def test_direct_engine_ingest_scrubs():
    m = MemorySystem(tempfile.mkdtemp(), agent_name="t")
    m.ingest(f"deploy {SECRET} call {PII} about the durable release run scheduled today please")
    _clean(m.memories[0].content if m.memories else "", "MemorySystem.ingest")


def test_scrub_pii_toggle_is_retired_pii_always_on_and_counter_is_accurate():
    """D1: `scrub_pii=False` is accepted for compatibility but has NO effect - PII scrub is always on
    end to end, and WriteResult.redactions reflects what actually landed (no under-count). GPT found
    the old toggle made the counter lie: 0 while the seam still redacted."""
    from parsica_spirit import open_markdown
    root = tempfile.mkdtemp(prefix="d1_pii_")
    ctx = open_markdown(root, scrub_pii=False)          # explicitly ask to disable - must be ignored
    res = ctx.store(f"call me at {PII} about the durable release run happening later today")
    assert res.stored and res.redactions >= 1, "PII redaction not counted (the counter lied)"
    stored = ctx.get(res.id)
    _clean(stored.content, "store content under scrub_pii=False")
    _clean(_disk(root), "disk under scrub_pii=False")
