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
  release. Held product mechanisms were removed before the port. The closure
  gate that verifies their absence (`tests/engine/test_closure.py`) ships and
  runs, but the list of excluded module names is itself a tell, so it does not
  ship: it lives in a site-local sidecar. On a public checkout the closure tests
  therefore SKIP visibly ("gate not armed") rather than pass over an empty set;
  the armed run happens release-side, where the sidecar is present, and the
  release verifier (`scripts/verify_release.py`) fails closed if it is not. The
  clean-room check independently proves the base tier runs with every engine
  import blocked at runtime.
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
server, and cloud layers remain private and are not part of this release. The
closure-gate machinery that enforces their absence ships and runs; the excluded
module names do not (they are a tell), so the gate is armed release-side, where
its site-local vocabulary lives, and the release verifier fails closed without it.
A public checkout sees the tests skip visibly, never a silent empty pass.

## A note on versioning

This is the **v1.0.0** public release - a new, standalone open-source product.
`pyproject.toml` is the source of truth for the version you have.

The engine was distilled from Antaris Analytics' larger internal memory system,
which carried its own, longer private development history. That internal lineage
is not this package's public version: a fresh public product starts at 1.0.0
regardless of how long the code it was cut from had evolved privately.
