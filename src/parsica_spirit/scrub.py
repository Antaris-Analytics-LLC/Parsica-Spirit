"""Parsica Spirit - content scrubber (re-export).

The scrubber now lives in the shared leaf (``parsica_spirit.enrich.scrub``) so BOTH the Context
facade and the engine run the identical scrub-before-store gate - there is no write path
around it. This module re-exports it for backward compatibility.
"""
from parsica_spirit.enrich.scrub import REDACTION, looks_clean, scrub

__all__ = ["scrub", "looks_clean", "REDACTION"]
