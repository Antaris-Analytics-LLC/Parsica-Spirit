"""S1 ingress gateway: no external ingress reaches disk unsanitized, and import is crash-safe.

GPT's common cause: schema-valid does not mean policy-safe. Externally supplied metadata
(a caller's memory_type / type_config, or an imported export file) must be canonicalised -
scrubbed, validated, bounded - BEFORE it can reach the decay/recall math, an on-disk label or
filename, or the persisted store. And import must be transactional: a malformed file must never
leave the store wiped or half-filled.

These tests are the ONLY coverage for import_from / export / type_config sanitisation (the
package had none), so they exercise both the memory_types validators directly and the whole
MemorySystem ingress surface end to end, walking the store to prove no secret survives.
"""
import json
import math
import os
import tempfile

from parsica_spirit.engine import MemorySystem
from parsica_spirit.engine.memory_types import (
    DEFAULT_TYPE, MEMORY_TYPE_CONFIGS,
    valid_memory_type, normalize_memory_type, sanitize_type_config, get_type_config,
)

# sk-ant-[A-Za-z0-9_-]{20,} -> caught by the anthropic_key token pattern.
SECRET = "sk-ant-api03-SECRETKEY1234567890abcdefGHIJKLMNOP"


def _ws(prefix):
    return tempfile.mkdtemp(prefix=prefix)


def _outside():
    """A dir OUTSIDE any store root, for import/export files that themselves carry a secret so
    the store walk never trips over the fixture file rather than a real leak."""
    return tempfile.mkdtemp(prefix="ingress_outside_")


def _leaks(root, needle):
    hits = []
    nb = needle.encode("utf-8")
    for dp, _d, files in os.walk(root):
        for name in files:
            rel = os.path.relpath(os.path.join(dp, name), root)
            if needle in rel:
                hits.append(rel)
            try:
                with open(os.path.join(dp, name), "rb") as fh:
                    if nb in fh.read():
                        hits.append(rel + ":CONTENT")
            except OSError:
                continue
    return hits


def _assert_clean(root):
    assert _leaks(root, SECRET) == [], "secret leaked to: %r" % _leaks(root, SECRET)


# -- memory_types validators (unit) -------------------------------------------

def test_valid_memory_type():
    assert valid_memory_type("episodic")
    assert valid_memory_type("my_custom.type-2")
    assert not valid_memory_type("MixedCase")       # must be lowercase-first slug
    assert not valid_memory_type("has space")
    assert not valid_memory_type("..\x07evil")
    assert not valid_memory_type("")
    assert not valid_memory_type(None)
    assert not valid_memory_type("x" * 65)          # over the 64-char cap


def test_normalize_memory_type_fails_safe_to_default():
    assert normalize_memory_type("fact") == "fact"
    assert normalize_memory_type("custom_slug") == "custom_slug"
    assert normalize_memory_type("..\x07EVIL/../etc") == DEFAULT_TYPE   # neutralised, not dropped
    assert normalize_memory_type(None) == DEFAULT_TYPE
    assert normalize_memory_type(12345) == DEFAULT_TYPE


def test_sanitize_type_config_allowlist_and_bounds():
    dirty = {
        "decay_multiplier": 1e9,          # clamp to 100
        "importance_boost": -5,           # clamp to 0
        "recall_priority": 0.75,          # in range, kept
        "label": "x" * 200,               # truncate to 64
        "evil_key": "arbitrary",          # dropped
        "__class__": "danger",            # dropped
    }
    out = sanitize_type_config(dirty)
    assert set(out) <= {"decay_multiplier", "importance_boost", "recall_priority", "label"}
    assert "evil_key" not in out and "__class__" not in out
    assert out["decay_multiplier"] == 100.0
    assert out["importance_boost"] == 0.0
    assert out["recall_priority"] == 0.75
    assert len(out["label"]) == 64


def test_sanitize_type_config_drops_nonfinite_and_noncoercible():
    for bad in (float("nan"), float("inf"), float("-inf"), "not a number", None, [1]):
        out = sanitize_type_config({"decay_multiplier": bad})
        assert "decay_multiplier" not in out, "non-finite/non-coercible numeric survived: %r" % (bad,)
    # a NaN must never reach the config the decay math consumes
    assert not any(isinstance(v, float) and not math.isfinite(v) for v in
                   sanitize_type_config({"recall_priority": float("nan")}).values())


def test_sanitize_type_config_scrubs_label():
    out = sanitize_type_config({"label": "team " + SECRET}, scrub=lambda s: s.replace(SECRET, "[REDACTED]"))
    assert SECRET not in out["label"]


def test_get_type_config_is_self_defending():
    # a hostile type with an override collapses to the canonical default - the override is ignored
    cfg = get_type_config("..\x07evil", {"decay_multiplier": 1e9, "evil": "x"})
    assert cfg == MEMORY_TYPE_CONFIGS[DEFAULT_TYPE]
    # a clean custom slug keeps the SANITISED override merged onto the episodic base
    cfg2 = get_type_config("custom_slug", {"decay_multiplier": 1e9, "evil": "x", "label": "My Team"})
    assert cfg2["decay_multiplier"] == 100.0     # clamped, not 1e9 (proves the override applied)
    assert "evil" not in cfg2                     # arbitrary key dropped
    assert cfg2["label"] == "My Team"             # sanitised label override applied


# -- import_from ingress + crash safety (integration) --------------------------

def test_import_from_sanitizes_hostile_entry():
    ws = _ws("ingress_import_")
    out = _outside()
    mem = MemorySystem(ws, agent_name="tester")

    hostile = {"entries": [{
        "content": "a memory carrying " + SECRET + " that must be scrubbed on import",
        "source": "src-" + SECRET,
        "category": "general",
        "memory_type": "..\x07EVIL/../etc",   # not a clean slug -> normalise to default
        "timestamp": "2026-01-01T00:00:00",
        "_hash": "deadbeefcafe",               # tampered -> must be ignored (recomputed)
        "type_metadata": {"leak": SECRET},     # hostile extra key -> must never be read
    }]}
    path = os.path.join(out, "hostile_export.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(hostile, fh)

    n = mem.import_from(path, merge=True)
    assert n == 1
    mem.flush()
    entry = mem.memories[0]
    assert SECRET not in entry.content
    assert SECRET not in (entry.source or "")
    assert entry.memory_type == DEFAULT_TYPE          # hostile type neutralised
    assert entry.hash != "deadbeefcafe"               # hash recomputed, file hash not trusted
    assert not entry.type_metadata                    # hostile extra key never consumed
    _assert_clean(ws)                                 # nothing secret anywhere under the store


def test_import_replace_never_wipes_on_empty_or_invalid():
    ws = _ws("ingress_nowipe_")
    out = _outside()
    mem = MemorySystem(ws, agent_name="tester")
    mem.ingest("an existing memory that must survive a bad replace-import here now", source="inline")
    mem.flush()
    before = len(mem.memories)
    assert before >= 1

    empty_path = os.path.join(out, "empty.json")
    with open(empty_path, "w", encoding="utf-8") as fh:
        json.dump({"entries": []}, fh)
    assert mem.import_from(empty_path, merge=False) == 0
    assert len(mem.memories) == before, "replace-import of an EMPTY file wiped the store"

    junk_path = os.path.join(out, "junk.json")
    with open(junk_path, "w", encoding="utf-8") as fh:
        json.dump({"entries": ["not a dict", 123, None]}, fh)
    assert mem.import_from(junk_path, merge=False) == 0
    assert len(mem.memories) == before, "replace-import of ALL-INVALID entries wiped the store"

    # a non-dict/non-list top level is also a no-op, never a wipe
    weird_path = os.path.join(out, "weird.json")
    with open(weird_path, "w", encoding="utf-8") as fh:
        json.dump("just a string", fh)
    assert mem.import_from(weird_path, merge=False) == 0
    assert len(mem.memories) == before, "replace-import of a non-collection wiped the store"


def test_clean_export_import_round_trips():
    ws1 = _ws("ingress_rt_src_")
    out = _outside()
    mem1 = MemorySystem(ws1, agent_name="tester")
    mem1.ingest_fact("a clean fact worth remembering across an export and a later reimport", source="inline")
    mem1.flush()
    assert any(m.memory_type == "fact" for m in mem1.memories), "sanity: a fact was stored"

    exp = os.path.join(out, "clean_export.json")
    mem1.export(exp)

    ws2 = _ws("ingress_rt_dst_")
    mem2 = MemorySystem(ws2, agent_name="tester")
    n = mem2.import_from(exp, merge=True)
    assert n >= 1
    assert any(m.memory_type == "fact" and "clean fact" in m.content for m in mem2.memories), \
        "legit data must round-trip with its type and content intact"


def test_self_reimport_deduplicates():
    """Re-importing an export back into its own store must NOT duplicate, even though the hash is
    recomputed (line reset) - the content-norm dedup catches it without trusting the file hash."""
    ws = _ws("ingress_selfrt_")
    out = _outside()
    mem = MemorySystem(ws, agent_name="tester")
    mem.ingest("a memory that will be exported and then re-imported into the same store", source="inline")
    mem.flush()
    before = len(mem.memories)
    exp = os.path.join(out, "self_export.json")
    mem.export(exp)
    added = mem.import_from(exp, merge=True)
    assert added == 0, "self-reimport created duplicates"
    assert len(mem.memories) == before


def test_constructor_scrubs_agent_name():
    ws = _ws("ingress_agent_")
    mem = MemorySystem(ws, agent_name="agent-" + SECRET)
    assert SECRET not in (mem.agent_name or ""), "agent_name retained a raw secret"
    mem.ingest("a memory stored under a secret-bearing agent name that must be scrubbed", source="inline")
    mem.flush()
    _assert_clean(ws)


def test_ingest_neutralizes_hostile_memory_type():
    ws = _ws("ingress_ingest_type_")
    mem = MemorySystem(ws, agent_name="tester")
    mem.ingest("a normal memory line long enough to actually be stored by the engine here",
               source="inline", memory_type="..\x07EVIL",
               type_config={"decay_multiplier": float("nan"), "evil_key": SECRET})
    mem.flush()
    assert mem.memories, "sanity: the memory was stored (hostile type never drops the data)"
    assert all(valid_memory_type(m.memory_type) for m in mem.memories), \
        "a hostile memory_type reached a stored entry"
    _assert_clean(ws)


# -- RC4 / GPT round-2: synthesize, credential-shaped type, imported timestamp ----------

def test_synthesize_scrubs_and_registers_external_research():
    """S1: synthesized research is fresh EXTERNAL text. It must be scrubbed AND fully registered
    (dedup/index) - a raw credential must never reach disk, and the same content must not later
    re-ingest as a non-duplicate (which proves the bookkeeping ran)."""
    ws = _ws("ingress_synth_")
    mem = MemorySystem(ws, agent_name="tester")
    mem.synthesize(research_results={
        "scan": "Research finding: the leaked key was " + SECRET + " and it recurs across the dump."})
    mem.save()
    _assert_clean(ws)
    synth = [m for m in mem.memories if "synthesis" in getattr(m, "tags", [])]
    assert synth, "no synthesized entry committed"
    s = synth[0]
    assert s.hash in mem._hashes, "synthesized entry not registered in _hashes (dedup bypass)"
    before = len(mem.memories)
    mem.ingest(s.content)                      # identical content must dedup, not duplicate
    assert len(mem.memories) == before, "synthesized content re-ingested as a non-duplicate"


def test_ingest_neutralizes_credential_shaped_memory_type():
    """S2a: a lowercase credential matches the slug regex, so the syntax check alone let it persist
    as a type. The gateway now scrub-collapses it to the default (data still stored)."""
    ws = _ws("ingress_credtype_")
    mem = MemorySystem(ws, agent_name="tester")
    mem.ingest("a normal memory line long enough to actually be stored by the engine here",
               source="inline", memory_type=SECRET.lower())
    mem.flush()
    assert mem.memories, "the memory was still stored"
    assert SECRET.lower() not in {getattr(m, "memory_type", "") for m in mem.memories}, \
        "a credential-shaped memory_type survived as an identifier"
    _assert_clean(ws)


def test_import_rejects_non_iso_timestamp_keeps_valid_verbatim():
    """S2b: an imported timestamp is untrusted. A non-ISO (credential-shaped) value must never
    persist raw; a valid ISO stamp survives verbatim (export->import round-trip stays byte-stable)."""
    out = _outside()
    bad = os.path.join(out, "bad.json")
    with open(bad, "w", encoding="utf-8") as fh:
        json.dump({"entries": [{"content": "an imported entry long enough to persist in the store",
                                "source": "ext", "timestamp": SECRET}]}, fh)
    ws = _ws("ingress_ts_")
    mem = MemorySystem(ws, agent_name="tester")
    mem.import_from(bad)
    mem.flush()
    _assert_clean(ws)
    ext = [m for m in mem.memories if m.source == "ext"]
    assert ext and ext[0].created != SECRET, "a credential-shaped timestamp persisted in `created`"

    good_ts = "2026-07-14T10:20:30.123456"
    ok = os.path.join(out, "good.json")
    with open(ok, "w", encoding="utf-8") as fh:
        json.dump({"entries": [{"content": "another imported entry long enough to persist now here",
                                "source": "ext2", "timestamp": good_ts}]}, fh)
    ws2 = _ws("ingress_ts_ok_")
    mem2 = MemorySystem(ws2, agent_name="tester")
    mem2.import_from(ok)
    ext2 = [m for m in mem2.memories if m.source == "ext2"]
    assert ext2 and ext2[0].created == good_ts, "a valid ISO timestamp was not preserved verbatim"
