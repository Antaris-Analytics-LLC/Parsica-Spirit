"""Coverage for the headline retrieval stack: multi-layer BM25+ search, entity
graph, and co-occurrence, wired on by default.

Each test exercises one accumulating-substrate feature through the public
surface and asserts on what is actually observable:

  1. BM25 ranking - the memory that matches a query term ranks first; an
     unrelated memory ranks lower or is absent.
  2. Entity graph (graph_intelligence=True default) - a proper-noun entity
     shared across two memories is recorded on self._graph with a mention
     count / entry list spanning both, plus a relationship edge.
  3. Co-occurrence - ingest populates the co-occurrence matrix and
     search_with_context(cooccurrence_boost=True) runs and returns results.

Deterministic: no clocks, randomness, sleeps, or network.
"""
from parsica_spirit.engine import MemorySystem


def _mem(tmp_path, name="ws", **kw):
    ws = tmp_path / name
    ws.mkdir(parents=True, exist_ok=True)
    return MemorySystem(str(ws), agent_name="tester", **kw)


# -- 1. BM25 ranking --------------------------------------------------------

def test_bm25_ranks_matching_memory_first(tmp_path):
    mem = _mem(tmp_path)
    kube = "Kubernetes pod autoscaling depends on CPU and memory utilization metrics"
    budget = "The quarterly budget review covered marketing spend and headcount figures"
    espresso = "The espresso machine in the office kitchen needs descaling every month"
    turtles = "Sea turtles migrate thousands of miles across the open Pacific ocean"
    for note in (kube, budget, espresso, turtles):
        assert mem.ingest(note) >= 1

    results = mem.search("kubernetes autoscaling metrics")
    assert results, "a matching query must return at least one result"
    contents = [r.content for r in results]

    # The clearly-matching memory is the top-1 hit.
    assert contents[0] == kube, contents

    # An unrelated memory ranks strictly lower, or does not appear at all.
    assert (espresso not in contents) or (contents.index(espresso) > contents.index(kube))
    assert (turtles not in contents) or (contents.index(turtles) > contents.index(kube))

    # A second, distinct query lands on its own memory - ranking is not fixed.
    espresso_hits = mem.search("espresso machine descaling kitchen")
    assert espresso_hits and espresso_hits[0].content == espresso


# -- 2. Entity graph ---------------------------------------------------------

def test_entity_graph_records_shared_entity_and_relationship(tmp_path):
    mem = _mem(tmp_path)
    assert mem._graph is not None, "graph_intelligence defaults on"

    assert mem.ingest("ProjectPhoenix depends on DataPipeline for nightly storage jobs") >= 1
    assert mem.ingest("The ProjectPhoenix rollout was approved by the platform team") >= 1

    # The shared proper-noun entity is a single node whose mention/entry
    # accounting spans BOTH ingested memories - that is the recorded association.
    node = mem._graph.get_node("ProjectPhoenix")
    assert node is not None, "shared entity must be present in the graph"
    assert node.canonical == "projectphoenix"
    assert node.mention_count >= 2, node
    assert len(set(node.entry_ids)) >= 2, node.entry_ids

    # The graph also recorded at least one relationship edge, and it links
    # the two co-mentioned entities.
    stats = mem._graph.get_stats()
    assert stats["node_count"] >= 2
    assert stats["edge_count"] >= 1, stats

    rels = mem._graph.relationship_search("ProjectPhoenix")
    assert rels, "relationship_search must surface edges for the entity"
    assert any(
        "projectphoenix" in (src, tgt) and "datapipeline" in (src, tgt)
        for src, _rel, tgt, _entries in rels
    ), rels


# -- 3. Co-occurrence ---------------------------------------------------------

def test_cooccurrence_matrix_populates_and_boost_runs(tmp_path):
    mem = _mem(tmp_path)
    assert mem.ingest("Hepatic inflammation elevated the patient ALT enzyme levels") >= 1
    assert mem.ingest("The liver function panel showed elevated ALT and hepatic markers") >= 1

    # Ingest populated the co-occurrence index (the assertable surface).
    cooc = mem._cooccurrence
    assert cooc.vocab_size > 0, "ingest must populate the co-occurrence vocabulary"
    assert cooc.total_pairs > 0, "ingest must record co-occurrence pairs"

    # A term seen near others yields co-occurrence neighbours.
    neighbours = cooc.similar_words("hepatic", top_k=5)
    assert neighbours, "a co-occurring term must have neighbours"
    assert all(isinstance(w, str) and score >= 0.0 for w, score in neighbours)

    # cooccurrence_boost is exercised end-to-end through the public path.
    results, ctx = mem.search_with_context("hepatic inflammation", cooccurrence_boost=True)
    assert results, "co-occurrence-boosted search must return results"

    # And it is a real toggle: the un-boosted path still returns results too.
    plain, _ = mem.search_with_context("hepatic inflammation", cooccurrence_boost=False)
    assert plain
