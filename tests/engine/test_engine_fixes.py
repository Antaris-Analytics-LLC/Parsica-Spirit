"""Regression tests for a set of engine-behavior invariants.

One test per invariant, asserting the documented behavior against the live engine:

  1. read-cache key includes confidence_floor (+ include_cold) - a floored
     search result must never be served from cache to an unfloored call
  2. search() tags / tag_mode filters actually narrow results
  3. search() date_range is inclusive over the creation date
  4. synthesize_knowledge() returns a summary string (used to TypeError)
  5. the instrumentation log never persists raw query text (PII or otherwise)
  6. delete_source() writes audit entries, mirroring forget()
"""
import json
import os
import tempfile
from datetime import datetime

from parsica_spirit.engine import MemorySystem


def _mem():
    ws = tempfile.mkdtemp(prefix="engine_fixes_")
    return MemorySystem(ws, agent_name="tester"), ws


def test_read_cache_key_includes_confidence_floor():
    mem, _ = _mem()
    assert mem.ingest("The backoff policy caps retries at sixty seconds") >= 1
    # A floor high enough to filter everything caches an EMPTY result set...
    assert mem.search("backoff policy", confidence_floor=999.0) == []
    # ...which must NOT be served to the unfiltered variant of the same query.
    full = mem.search("backoff policy")
    assert full, "unfiltered search must not inherit the floored call's cached empty set"


def test_search_tags_filter_any_and_all():
    mem, _ = _mem()
    assert mem.ingest("Alpha deploy checklist for the primary cluster",
                      tags=["deploy", "alpha"]) >= 1
    assert mem.ingest("Beta deploy checklist for the backup cluster",
                      tags=["deploy", "beta"]) >= 1
    assert mem.ingest("Gamma release notes for the archive shelf",
                      tags=["notes"]) >= 1

    # "any" (default): at least one requested tag
    hits = mem.search("deploy checklist cluster", tags=["alpha"])
    assert hits, "tags filter must not blank out matching results"
    assert all("alpha" in (getattr(e, "tags", None) or []) for e in hits)

    # "all": every requested tag
    both = mem.search("deploy checklist cluster", tags=["deploy", "beta"], tag_mode="all")
    assert both
    assert all({"deploy", "beta"} <= set(getattr(e, "tags", None) or []) for e in both)

    # a tag nothing carries filters everything out
    assert mem.search("deploy checklist cluster", tags=["nonexistent-tag"]) == []


def test_search_date_range_inclusive_and_excludes_past():
    mem, _ = _mem()
    assert mem.ingest("Quarterly planning review happened with the leadership team") >= 1
    today = datetime.now().date().isoformat()
    # inclusive: an entry created today matches a (today, today) range -
    # guards the created[:10] date truncation against full-ISO comparison
    hits = mem.search("quarterly planning review", date_range=(today, today))
    assert hits, "an entry created today must match an inclusive (today, today) range"
    # a range far in the past excludes it
    assert mem.search("quarterly planning review",
                      date_range=("2000-01-01", "2000-01-02")) == []


def test_synthesize_knowledge_returns_summary_string():
    mem, _ = _mem()
    assert mem.ingest("The release process uses a two-step verification gate") >= 1
    out = mem.synthesize_knowledge("release process verification")
    assert isinstance(out, str), "synthesize_knowledge must return a string on hits"
    assert "Knowledge synthesis" in out and "two-step" in out
    # no matches -> None, per the Optional[str] contract
    assert mem.synthesize_knowledge("zzzz qqqq xxxx") is None


def test_instrumentation_log_query_privacy():
    mem, ws = _mem()
    assert mem.ingest("Deploy checklist for the primary service") >= 1
    log_path = os.path.join(ws, ".instrumentation_log.jsonl")

    # The retrieved event NEVER carries raw query text.
    mem.search_with_context("deploy checklist for admin@example.com")
    with open(log_path, encoding="utf-8") as f:
        events = [json.loads(line) for line in f]
    retrieved = [e for e in events if e.get("event") == "retrieved"]
    assert retrieved, "search_with_context must log a retrieved event"
    assert all("query" not in e for e in retrieved), \
        "raw query text must never be persisted to the instrumentation log"
    assert not any("admin@example.com" in json.dumps(e) for e in retrieved), \
        "no query text (PII or otherwise) may reach the instrumentation log"


def test_delete_source_writes_audit_entries():
    mem, ws = _mem()
    assert mem.ingest("Imported document content about caching layers",
                      source_url="https://example.com/doc") >= 1
    res = mem.delete_source("https://example.com/doc")
    assert res["deleted"] >= 1, res
    audit_path = os.path.join(ws, "memory_audit.json")
    assert os.path.exists(audit_path), "delete_source must write to the audit log"
    with open(audit_path, encoding="utf-8") as f:
        text = f.read()
    assert '"delete_source"' in text and "https://example.com/doc" in text
