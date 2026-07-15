# Changelog

All notable changes to Parsica Spirit are documented in this file.
This project follows [Semantic Versioning](https://semver.org).

## [1.0.0] - 2026-07-10

Initial public release.

Parsica Spirit is a model-free, local-first persistent memory engine for AI agents:
deterministic storage, layered BM25+ retrieval, decay and consolidation, a graph +
co-occurrence + PPMI semantic substrate, and a scrub-gated markdown continuity layer -
built on the Python standard library, with zero runtime dependencies.

One package (`parsica-spirit`), one install, three zones inside:

- **the facade** - the markdown continuity adapter: the scrub gateway, identity,
  routing, and promotion.
- **`parsica_spirit.engine`** - the memory engine: storage, search, decay,
  consolidation, graph intelligence, co-occurrence / PPMI, tiered storage, and recovery.
- **`parsica_spirit.enrich`** - the shared storage-seam and enrichment contracts leaf.
