"""Tests for the deterministic reference enricher."""

from parsica_spirit.enrich import EnricherPort, EnrichmentRecord, ReferenceEnricher


def test_satisfies_enricher_port():
    assert isinstance(ReferenceEnricher(), EnricherPort)


def test_deterministic_same_input_same_output():
    e = ReferenceEnricher()
    t = "Bruxism is teeth grinding at night. Bruxism wears enamel. Night guards help bruxism."
    assert e.enrich(t) == e.enrich(t)


def test_produces_valid_nonempty_record():
    r = ReferenceEnricher().enrich("Bruxism is teeth grinding at night. Bruxism wears enamel.")
    assert isinstance(r, EnrichmentRecord)
    assert not r.is_empty()
    assert r.keywords[0] == "bruxism"  # most frequent content word ranks first
    assert len(r.search_queries) <= 3
    assert len(r.tags) <= 5


def test_empty_and_none_text_are_safe():
    assert ReferenceEnricher().enrich("").is_empty()
    assert ReferenceEnricher().enrich(None).is_empty()  # type: ignore[arg-type]
