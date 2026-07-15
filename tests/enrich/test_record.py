"""Tests for the EnrichmentRecord contract: round-trip, tolerance, immutability."""

import pytest

from parsica_spirit.enrich import EnrichmentRecord, SCHEMA_VERSION


def test_roundtrip_zero_loss():
    r = EnrichmentRecord(
        summary="a sleep-bruxism note",
        tags=("bruxism", "night_guard"),
        keywords=("teeth grinding",),
        search_queries=("q1", "q2", "q3"),
    )
    assert EnrichmentRecord.from_dict(r.to_dict()) == r


def test_closed_on_write_exact_fields():
    keys = set(EnrichmentRecord(summary="x").to_dict().keys())
    assert keys == {"schema_version", "summary", "tags", "keywords", "search_queries"}


def test_tolerant_on_read_coerces_odd_shapes():
    r = EnrichmentRecord.from_dict(
        {"summary": None, "tags": "single", "keywords": [1, 2, None, ""], "search_queries": None, "extra": "ignored"}
    )
    assert r.summary == ""
    assert r.tags == ("single",)
    assert r.keywords == ("1", "2")  # ints coerced, None and "" dropped
    assert r.search_queries == ()


@pytest.mark.parametrize("bad", [None, {}, {"tags": 5}, {"summary": 123}, {"schema_version": "nope"}, "not-a-dict-ish"])
def test_from_dict_never_raises_on_garbage(bad):
    # tolerant-on-read must never be the thing that breaks a load
    EnrichmentRecord.from_dict(bad if isinstance(bad, dict) or bad is None else {})


def test_is_frozen():
    r = EnrichmentRecord(summary="x")
    with pytest.raises(Exception):
        r.summary = "y"  # type: ignore[misc]


def test_schema_version_pinned_and_travels():
    assert EnrichmentRecord().schema_version == SCHEMA_VERSION
    assert EnrichmentRecord.from_dict({"summary": "x"}).schema_version == SCHEMA_VERSION
    assert EnrichmentRecord.from_dict({"schema_version": 7}).schema_version == 7


def test_is_empty():
    assert EnrichmentRecord().is_empty()
    assert EnrichmentRecord.from_dict({}).is_empty()
    assert not EnrichmentRecord(tags=("x",)).is_empty()
    assert not EnrichmentRecord(summary="x").is_empty()
