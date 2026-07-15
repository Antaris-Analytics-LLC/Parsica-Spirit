"""parsica_spirit.enrich - the shared CONTRACT leaf for Parsica Spirit.

Zero dependencies. Zero sibling imports. The single source of truth for what an enrichment
*is* (doc2query), the storage seam (StoragePort / AdminBackend), the shared reference
backend kit (PlanLedger + helpers), AND the conformance suite - all consumed identically by
the Claude Code adapter and the engine, which is what keeps the dependency graph acyclic
(context -> enrich <- memory). The engine backend imports its plan-ledger and is tested by
the conformance suite from HERE, never from the adapter.
"""

from .record import SCHEMA_VERSION, EnrichmentRecord
from .ports import EnricherPort
from .reference import ReferenceEnricher
from .batch import ENRICH_ITEM_KEYS, build_batch, parse_batch
from .storage import (
    DISPOSITIONS,
    AdminBackend,
    PlanError,
    ResetPlan,
    StoragePort,
    StoredRecord,
)
from .backend_kit import PlanLedger, content_id, labels_match, searchable, tokens
from .conformance import run_storage_conformance
from .scrub import REDACTION, looks_clean, scrub, scrub_write

__all__ = [
    # enrichment
    "EnrichmentRecord", "EnricherPort", "ReferenceEnricher",
    "build_batch", "parse_batch", "ENRICH_ITEM_KEYS", "SCHEMA_VERSION",
    # storage seam
    "StoragePort", "AdminBackend", "StoredRecord", "ResetPlan", "PlanError", "DISPOSITIONS",
    # shared backend kit + conformance (reference backends AND the engine backend)
    "PlanLedger", "tokens", "searchable", "content_id", "labels_match",
    "run_storage_conformance",
    # content scrubber - shared by the Spirit Context facade AND the engine
    "scrub", "looks_clean", "REDACTION", "scrub_write",
]
__version__ = "1.0.0"
