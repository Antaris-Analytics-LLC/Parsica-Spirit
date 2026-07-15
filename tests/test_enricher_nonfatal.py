"""Regression: a bring-your-own enricher that raises must NOT lose the write.

Enrichment is an optional overlay, not part of the durability contract: the
facade matches the engine's non-fatal ingest path by logging the failure and
persisting the CLEAN (scrubbed) text unenriched. Shipped untested until the
2026-07-08 release fact-check caught the facade aborting the whole store().
"""
import tempfile

from parsica_spirit import open_markdown


class _BoomEnricher:
    """An EnricherPort whose enrich() always fails (simulated outage)."""

    def enrich(self, text):
        raise RuntimeError("simulated enricher outage")


def test_raising_enricher_stores_unenriched():
    ctx = open_markdown(tempfile.mkdtemp(prefix="nonfatal_"), enricher=_BoomEnricher())
    res = ctx.store("a real note about bruxism at night")
    assert res.stored is True and res.id, "the write must land despite the enricher failure"
    got = ctx.get(res.id)
    assert got is not None
    assert got.enrichment is None, "a failed enrichment stores the record unenriched"
    # and the record is queryable like any other
    hits = ctx.query("bruxism")
    assert any(r.id == res.id for r in hits)


def test_enrich_false_still_skips_cleanly_with_raising_enricher():
    # enrich=False must never even invoke the enricher, raising or not.
    ctx = open_markdown(tempfile.mkdtemp(prefix="nonfatal_skip_"), enricher=_BoomEnricher())
    res = ctx.store("plain note stored with enrichment disabled", enrich=False)
    assert res.stored is True and res.id
    got = ctx.get(res.id)
    assert got is not None and got.enrichment is None
