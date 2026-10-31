# Provenance

This repository is an open-core release by **Antaris Analytics**, which owns or
controls the copyright in all included code and releases it under **Apache-2.0**
(see `LICENSE` and `NOTICE`). This note records where each layer comes from, so the
lineage is explicit rather than implied.

## Code

- **`src/parsica_spirit/engine`** (the engine): ported from Antaris Analytics'
  internal, Antaris-owned memory engine, then reduced to the lean open-core
  surface and folded into the `parsica_spirit` package as its engine subpackage.
  The port was intentional: this engine tier was always planned for open
  release. Held product mechanisms were removed before the port. A closure gate
  once checked their absence against a list of module names that was never in
  this repository; it was retired in October 2026, and `REVIEW-2026-10.md`
  records the reason. The clean-room check independently proves the base tier
  runs with every engine import blocked at runtime.
- **The facade and the contracts leaf** (the top-level `parsica_spirit` modules
  and `src/parsica_spirit/enrich`): newly written for this release. An
  independent similarity audit against all prior internal code found no flagged
  pairs (maximum observed similarity was ambient, under 0.15).

## Data

- **No user or corpus data ships.** The accumulating substrate (runtime indexes,
  graphs, co-occurrence statistics, calibration) starts empty by design and is built
  from **your** usage. Nothing here seeds your substrate with anyone else's data.

The Claude Code memory importer and the `StoragePort` / `AdminBackend` injection contract are
intentionally-opened runtime integration surfaces of the engine.

## Deliberately excluded

The private upstream's advanced routing, team, calibration-tooling, document-ingest,
server, and cloud layers remain private and are not part of this release. None of
those modules are present in this public tree. The closure-gate machinery that once
checked for them against a list kept outside the repository was retired in October
2026 (`REVIEW-2026-10.md`); the clean-room check still proves that the wheel carries
only the `parsica_spirit` package and that the base tier runs with every engine import
blocked.

## A note on versioning

This is the **v1.0.0** public release - a new, standalone open-source product.
`pyproject.toml` is the source of truth for the version you have.

The engine was distilled from Antaris Analytics' larger internal memory system,
which carried its own, longer private development history. That internal lineage
is not this package's public version: a fresh public product starts at 1.0.0
regardless of how long the code it was cut from had evolved privately.
