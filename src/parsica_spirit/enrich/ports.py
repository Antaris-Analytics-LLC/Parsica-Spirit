"""parsica_spirit.enrich.ports - the EnricherPort protocol.

An *enricher* is any callable object that maps opaque text to an
EnrichmentRecord. It is deliberately Claude-Code-agnostic and substrate-agnostic:
the leaf defines the SHAPE of the contract, not how it is fulfilled. Orchestration,
API keys, retries, batching, and any swarm dispatch live in the CALLER
(the facade or the engine), never in the leaf.

This keeps parsica_spirit.enrich a true leaf: zero imports from the rest of the
package, zero knowledge of where the text came from or how the record gets produced.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .record import EnrichmentRecord


@runtime_checkable
class EnricherPort(Protocol):
    """Opaque text in, a validated EnrichmentRecord out."""

    def enrich(self, text: str) -> EnrichmentRecord:
        ...
