"""Re-export of the shared backend kit, canonically defined in parsica_spirit.enrich.backend_kit.

Lives in the leaf so the engine backend can use the same PlanLedger + helpers without
importing the adapter. This module is a convenience alias for the adapter's own backends.
"""

from parsica_spirit.enrich.backend_kit import PlanLedger, content_id, labels_match, searchable, tokens

__all__ = ["PlanLedger", "content_id", "labels_match", "searchable", "tokens",
           "NATIVE_MEMORY_SOURCE", "NATIVE_MEMORY_WEIGHT", "record_weight"]

# Native-memory permanence class (base-tier backends only; the engine backend keeps its
# own ranking). Records imported from the host agent's own memory files carry
# source=claude-code-memory (claude_memory.SOURCE); ranked retrieval multiplies their
# overlap score by a FIXED weight - boosted, so an operator-curated note outranks an
# equal-overlap session record, and beatable, so a session hit with more than
# NATIVE_MEMORY_WEIGHT times the overlap still wins. The weight is a module constant:
# visible right here, pinned by test, and applied as a pure function of the record's
# labels, which preserves query purity (same store + same query -> same order, always).
NATIVE_MEMORY_SOURCE = "claude-code-memory"
NATIVE_MEMORY_WEIGHT = 2


def record_weight(labels) -> int:
    """The ranked-query weight of one record: NATIVE_MEMORY_WEIGHT for the native
    permanence class, 1 for everything else. A pure function of the labels."""
    lb = labels or {}
    return NATIVE_MEMORY_WEIGHT if lb.get("source") == NATIVE_MEMORY_SOURCE else 1
