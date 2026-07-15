"""Re-export of the storage conformance suite, canonically in parsica_spirit.enrich.conformance.

The conformance suite is the executable spec of the storage contract, so it lives with the
contract in the leaf - both the adapter's reference backends and the engine backend are
tested against this one suite.
"""

from parsica_spirit.enrich.conformance import run_storage_conformance

__all__ = ["run_storage_conformance"]
