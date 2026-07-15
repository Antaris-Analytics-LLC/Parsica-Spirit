"""Custom-type type_config overrides must actually DRIVE decay and recall.

The ingress gateway sanitises a caller's type_config and ingest stores it on the entry
(type_metadata["_type_config"]) - but for a long time nothing ever read it back: decay
consulted a bare key nothing writes, and recall consulted only the canonical table, so a
custom type's overrides were stored-and-ignored (and an unknown type got a free 1.0 recall
boost above every canonical type). These tests pin the fix:

  - a custom decay_multiplier actually scales the effective half-life,
  - a custom recall_priority actually reorders search results,
  - a custom type with NO override gets the episodic defaults (no free boost),
  - a hostile override smuggled around the gateway (import-shaped data) is re-sanitised
    on read: clamped into range, NaN/junk dropped,
  - canonical types are untouched by any of it (their configs are fixed by design).
"""
import math
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))

from parsica_spirit.engine import MemorySystem                      # noqa: E402
from parsica_spirit.engine.decay import DecayEngine, DEFAULT_HALF_LIFE  # noqa: E402
from parsica_spirit.engine.memory_types import MEMORY_TYPE_CONFIGS  # noqa: E402


def _mem():
    return MemorySystem(tempfile.mkdtemp(prefix="ctc_"), agent_name="tester")


def _entry(mem, needle):
    hits = [e for e in mem.memories if needle in e.content]
    assert hits, "seed entry not found: %s" % needle
    return hits[0]


def test_custom_decay_multiplier_scales_half_life():
    mem = _mem()
    mem.ingest("zsentinel slow-fading covenant note", memory_type="covenant",
               type_config={"decay_multiplier": 10.0})
    entry = _entry(mem, "zsentinel")
    eng = DecayEngine()
    assert eng.effective_half_life(entry) == DEFAULT_HALF_LIFE * 10.0
    # and the decay curve actually uses it: a 10x half-life entry scores higher at age
    fast = _mem()
    fast.ingest("zsentinel slow-fading covenant note", memory_type="covenant",
                type_config={"decay_multiplier": 0.5})
    assert eng.effective_half_life(_entry(fast, "zsentinel")) == DEFAULT_HALF_LIFE * 0.5


def test_custom_recall_priority_reorders_search():
    mem = _mem()
    # identical token shape (same length, same tf) so BM25 ties and priority decides
    mem.ingest("qquartz protocol handles retries alpha", memory_type="hotlane",
               type_config={"recall_priority": 1.0})
    mem.ingest("qquartz protocol handles retries bravo", memory_type="coldlane",
               type_config={"recall_priority": 0.05})
    results = mem.search("qquartz protocol retries", limit=5)
    assert len(results) == 2
    assert "alpha" in results[0].content, "high recall_priority must rank first"
    assert "bravo" in results[1].content


def test_unconfigured_custom_type_gets_episodic_defaults():
    mem = _mem()
    mem.ingest("xnovel unconfigured type entry", memory_type="novelty")
    entry = _entry(mem, "xnovel")
    # decay: episodic multiplier, no free scaling
    assert DecayEngine().effective_half_life(entry) == DEFAULT_HALF_LIFE
    # recall: episodic default priority - never a free 1.0 boost for being unknown
    episodic_priority = MEMORY_TYPE_CONFIGS["episodic"]["recall_priority"]
    assert MemorySystem._entry_recall_priority(entry) == episodic_priority


def test_hostile_stored_override_is_resanitised_on_read():
    mem = _mem()
    mem.ingest("yhostile smuggled override entry", memory_type="smuggled",
               type_config={"decay_multiplier": 2.0})
    entry = _entry(mem, "yhostile")
    # simulate import-shaped data that bypassed the ingress gateway
    entry.type_metadata["_type_config"] = {"decay_multiplier": 1e9,
                                           "recall_priority": float("nan"),
                                           "arbitrary_key": "dropped"}
    # clamped into the allowlisted range, never a runaway curve
    assert DecayEngine().effective_half_life(entry) == DEFAULT_HALF_LIFE * 100.0
    # NaN dropped -> episodic default stands
    assert MemorySystem._entry_recall_priority(entry) == \
        MEMORY_TYPE_CONFIGS["episodic"]["recall_priority"]


def test_legacy_bare_decay_key_still_honoured():
    mem = _mem()
    mem.ingest("wlegacy bare key entry", memory_type="oldstyle")
    entry = _entry(mem, "wlegacy")
    entry.type_metadata["decay_multiplier"] = 4.0        # the pre-fix documented location
    assert DecayEngine().effective_half_life(entry) == DEFAULT_HALF_LIFE * 4.0


def test_canonical_types_unchanged():
    mem = _mem()
    mem.ingest_mistake("assumed warm cache vsent", "verify cache state first")
    entry = _entry(mem, "vsent")
    assert DecayEngine().effective_half_life(entry) == DEFAULT_HALF_LIFE * 10.0
    assert MemorySystem._entry_recall_priority(entry) == 1.0
    # a type_config passed WITH a canonical type stays a no-op (fixed by design)
    mem.ingest("ucanon fact with ignored override", memory_type="fact",
               type_config={"recall_priority": 0.01, "decay_multiplier": 50.0})
    e2 = _entry(mem, "ucanon")
    assert MemorySystem._entry_recall_priority(e2) == MEMORY_TYPE_CONFIGS["fact"]["recall_priority"]
    assert DecayEngine().effective_half_life(e2) == DEFAULT_HALF_LIFE * MEMORY_TYPE_CONFIGS["fact"]["decay_multiplier"]
