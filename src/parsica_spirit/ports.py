"""Re-export of the storage seam contracts.

These are canonically defined in the shared leaf (parsica_spirit.enrich.storage) so that both
the adapter and the engine implement them by importing only the leaf - no sibling edge.
This module is a convenience alias for adapter-side callers.
"""

from parsica_spirit.enrich import (
    DISPOSITIONS,
    AdminBackend,
    PlanError,
    ResetPlan,
    StoragePort,
    StoredRecord,
)

__all__ = ["StoragePort", "AdminBackend", "StoredRecord", "ResetPlan", "PlanError", "DISPOSITIONS"]
