"""Metadata + enrichment scrub: no raw secret or PII survives on ANY persisted surface.

The per-line content scrub only cleans the memory TEXT. Everything else the engine
persists - labels/ids/provenance/channel, tags, type_config, the shard FILENAME (derived
from tags), the search/tag indexes, the WAL, and enricher output (summary/tags/keywords/
search_queries/facts) - used to be written verbatim. These tests drive each ingest entry
point, flush, then os.walk the WHOLE store and assert the secret AND the phone are absent
from every file's PATH and CONTENTS. Each fails without the metadata-scrub fix in core.py.
"""
import os
import tempfile

from parsica_spirit.engine import MemorySystem

# sk-ant-[A-Za-z0-9_-]{20,} -> caught by the anthropic_key token pattern.
SECRET = "sk-ant-api03-SECRETKEY1234567890abcdefGHIJKLMNOP"
# NNN-NNN-NNNN -> caught by the PII phone pattern (scrub_pii=True).
PHONE = "555-123-4567"


def _ws(prefix):
    return tempfile.mkdtemp(prefix=prefix)


def _leaks(root, needle):
    """Return every persisted location (relpath or 'relpath:CONTENT') that carries needle."""
    hits = []
    nb = needle.encode("utf-8")
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            if needle in rel:
                hits.append(rel)  # leak in a file/dir NAME (e.g. shard topic from a tag)
            path = os.path.join(dirpath, name)
            try:
                with open(path, "rb") as fh:
                    blob = fh.read()
            except OSError:
                continue
            if nb in blob:
                hits.append(rel + ":CONTENT")
    return hits


def _assert_clean(root):
    assert _leaks(root, SECRET) == [], "secret leaked to: %r" % _leaks(root, SECRET)
    assert _leaks(root, PHONE) == [], "phone leaked to: %r" % _leaks(root, PHONE)


def test_direct_metadata_surfaces_scrubbed():
    """ingest(): source, category, agent_id, session_id, source_url, content_hash,
    source_channel, and tags must all be scrubbed before storage."""
    ws = _ws("meta_direct_")
    mem = MemorySystem(ws, agent_name="tester")
    mem.ingest(
        "an ordinary memory line that is definitely long enough to store",
        source=SECRET,
        category="general",
        agent_id="agent-" + PHONE,
        session_id="sess-" + SECRET,
        tags=[SECRET, "call-" + PHONE],
        source_url="https://example.com/?token=" + SECRET,
        content_hash=SECRET,
        source_channel="chan-" + PHONE,
    )
    mem.flush()
    assert len(mem.memories) >= 1, "sanity: the memory must have been stored"
    _assert_clean(ws)


def test_ingest_mistake_type_metadata_scrubbed():
    """ingest_mistake(): what_happened/correction/root_cause/tags/source/agent_id/
    session_id are scrubbed, and type_metadata is built from the SCRUBBED values."""
    ws = _ws("meta_mistake_")
    mem = MemorySystem(ws, agent_name="tester")
    entry = mem.ingest_mistake(
        what_happened="leaked " + SECRET + " in a log line long enough to persist",
        correction="rotate the key immediately after the incident review",
        root_cause="hardcoded key and phone " + PHONE + " left in the config",
        tags=[SECRET, PHONE],
        source="src-" + SECRET,
        agent_id="agent-" + PHONE,
        session_id="sess-" + SECRET,
    )
    mem.flush()
    assert entry is not None, "sanity: the mistake must have been stored"
    # The in-memory type_metadata must itself be clean (it is persisted verbatim).
    tm = " ".join(str(v) for v in entry.type_metadata.values())
    assert SECRET not in tm and PHONE not in tm, "type_metadata retained raw secret/PII"
    _assert_clean(ws)


def test_enricher_outputs_scrubbed():
    """enrichment: a custom enricher echoing the secret/phone in summary/tags/keywords/
    search_queries/facts must not land raw on any surface (fields, indexes, decomposed facts)."""
    ws = _ws("meta_enrich_")

    def enricher(_text):
        return {
            "summary": "summary that mentions " + SECRET,
            "tags": ["leak", PHONE],
            "keywords": [SECRET, "keyword"],
            "search_queries": ["how did " + SECRET + " get exposed"],
            "facts": ["the secret " + SECRET + " was exposed via phone " + PHONE + " somehow"],
        }

    mem = MemorySystem(ws, agent_name="tester", enricher=enricher)
    mem._decomposition_enabled = True  # exercise the fact-decomposition path too
    mem.ingest("a normal enough memory line worth storing and enriching here", source="inline")
    mem.flush()
    # Sanity: enrichment ran and decomposition produced at least one fact entry.
    assert mem.get_enrichment_count() >= 1, "sanity: the enricher must have run"
    assert any(getattr(m, "memory_type", "") == "fact" for m in mem.memories), \
        "sanity: decomposition must have created a fact entry"
    _assert_clean(ws)


# Two batches of decomposed facts: one whose facts closely resemble one another, one whose
# facts do not. Decomposition assigns a fixed confidence, so both batches return the same value.
_RESEMBLING_FACTS = [
    "the pipeline bridge watchdog timeout was set to five seconds during the run",
    "the pipeline bridge watchdog timeout was set to five seconds in the config file",
]
_UNRELATED_FACTS = [
    "version 5.3.0 was published to the package index in the spring release cycle",
    "the storage backend keeps a write ahead log so it survives a mid write crash",
]


def _decomposed_confidences(ws, facts):
    """Ingest one parent memory through an enricher that emits `facts`, then return the
    confidence of every entry the decomposition step produced."""
    def enricher(_text):
        return {"facts": list(facts)}
    mem = MemorySystem(ws, agent_name="tester", enricher=enricher)
    mem._decomposition_enabled = True
    mem.ingest("a parent memory line long enough to store and then decompose into facts",
               source="inline")
    mem.flush()
    return [m.confidence for m in mem.memories if getattr(m, "provenance", "") == "decomposed"]


def test_decomposed_fact_confidence_is_fixed():
    """Decomposition assigns each atomic fact a fixed confidence, set by the decomposition step
    and independent of the fact's content. A batch of closely-resembling facts and a batch of
    unrelated facts both come back with the one same value."""
    resembling = _decomposed_confidences(_ws("decomp_conf_a_"), _RESEMBLING_FACTS)
    unrelated = _decomposed_confidences(_ws("decomp_conf_b_"), _UNRELATED_FACTS)
    assert len(resembling) == 2 and len(unrelated) == 2, \
        "sanity: both facts in each batch must be stored (resembling=%r unrelated=%r)" \
        % (resembling, unrelated)
    assert set(resembling) | set(unrelated) == {0.8}, \
        "decomposed-fact confidence is not a single fixed value across batches: " \
        "resembling=%r unrelated=%r" % (resembling, unrelated)
