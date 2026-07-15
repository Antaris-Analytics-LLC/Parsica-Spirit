"""The engine dedups by EXACT normalized content only - never fuzzy/overlap.

Bloat prevention removes true duplicates; memories that merely share wording but
differ in content are all kept, at ingest and at compact() time.
"""
import tempfile

from parsica_spirit.engine import MemorySystem


def _mem():
    return MemorySystem(
        tempfile.mkdtemp(), agent_name="tester", use_sharding=False,
        use_indexing=False, tiered_storage=False, graph_intelligence=False,
    )


def test_compact_keeps_overlap_similar_facts():
    m = _mem()
    assert m.ingest("retry backoff doubles until sixty seconds", memory_type="fact") == 1
    assert m.ingest("retry backoff doubles until sixty seconds failure", memory_type="fact") == 1

    before = [x.content for x in m.memories if x.memory_type == "fact"]
    assert len(before) == 2

    m.compact()

    after = [x.content for x in m.memories if x.memory_type == "fact"]
    assert len(after) == 2, "overlap-similar but distinct facts must all be kept"


def test_compact_removes_exact_duplicate_facts():
    m = _mem()
    m.ingest("water is wet and blue", memory_type="fact")
    m.ingest("Water  is   WET and blue", memory_type="fact")  # identical after normalization

    n_before = len([x for x in m.memories if x.memory_type == "fact"])
    m.compact()
    n_after = len([x for x in m.memories if x.memory_type == "fact"])
    assert n_after < n_before, "true (exact) duplicates should collapse for bloat prevention"


def test_find_duplicates_is_exact_not_overlap():
    from parsica_spirit.engine.consolidation import ConsolidationEngine
    from parsica_spirit.engine.entry import MemoryEntry

    a = MemoryEntry(content="alpha beta gamma delta epsilon zeta")
    b = MemoryEntry(content="alpha beta gamma delta epsilon THETA")  # high overlap, not identical
    c = MemoryEntry(content="alpha beta gamma delta epsilon zeta")   # exact dup of a

    pairs = ConsolidationEngine().find_duplicates([a, b, c])
    flat = {id(x) for pair in pairs for x in pair}
    assert id(b) not in flat, "overlap-similar entry must not be treated as a duplicate"
    assert len(pairs) == 1, "only the exact duplicate pair is reported"
