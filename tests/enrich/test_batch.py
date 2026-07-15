"""Tests for the swarm batch envelope (build_batch / parse_batch)."""

import pytest

from parsica_spirit.enrich import EnrichmentRecord, build_batch, parse_batch


def test_build_batch_indexes():
    assert build_batch(["x", "y", "z"]) == [
        {"i": 0, "text": "x"},
        {"i": 1, "text": "y"},
        {"i": 2, "text": "z"},
    ]


def test_build_batch_handles_none():
    assert build_batch([None, "y"]) == [{"i": 0, "text": ""}, {"i": 1, "text": "y"}]


def test_parse_batch_is_dense_and_placed_by_i():
    raw = {"enrichments": [
        {"i": 2, "summary": "two", "tags": ["t"]},
        {"i": 0, "summary": "zero", "keywords": ["k"]},
    ]}
    out = parse_batch(raw, 3)
    assert len(out) == 3
    assert out[0].summary == "zero"
    assert out[1].is_empty()          # missing item -> empty record, no shift
    assert out[2].summary == "two"


def test_parse_batch_accepts_bare_list():
    assert parse_batch([{"i": 0, "summary": "a"}], 1)[0].summary == "a"


@pytest.mark.parametrize(
    "raw",
    [None, {}, [], "nope", {"enrichments": "bad"}, {"enrichments": [None, 5, {"i": "x"}, {"i": 99}]}],
)
def test_parse_batch_tolerant_of_garbage(raw):
    out = parse_batch(raw, 2)
    assert len(out) == 2
    assert all(isinstance(r, EnrichmentRecord) for r in out)
    assert all(r.is_empty() for r in out)
