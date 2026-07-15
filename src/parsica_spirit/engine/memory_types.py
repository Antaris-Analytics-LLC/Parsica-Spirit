"""
Memory type configurations for the Parsica Spirit engine.

Five canonical memory types with distinct decay and recall behaviour:

  episodic   - default, normal decay, normal recall priority
  fact       - normal decay, high recall priority
  preference - slow decay (3x half-life), high recall when context-matched
  procedure  - slow decay (3x half-life), high recall when task-matched
  mistake    - very slow decay (10x half-life), always surfaced first, 2x importance

Custom (non-canonical) types are supported via type_config dicts with the
same keys. Passing type_config for a canonical type has no effect: the five
built-in configs above are fixed by design.
"""

import math
import re
from typing import Callable, Dict, Optional

# -- canonical type definitions ------------------------------------------------

MEMORY_TYPE_CONFIGS: Dict[str, Dict] = {
    "episodic": {
        "decay_multiplier": 1.0,   # Normal half-life
        "importance_boost": 1.0,   # No boost
        "recall_priority": 0.5,    # Normal
        "label": "Episodic",
    },
    "fact": {
        "decay_multiplier": 1.0,   # Normal half-life
        "importance_boost": 1.2,   # Slight boost
        "recall_priority": 0.7,    # High
        "label": "Fact",
    },
    "preference": {
        "decay_multiplier": 3.0,   # 3x half-life = slower decay
        "importance_boost": 1.2,
        "recall_priority": 0.7,    # High when context-matched
        "label": "Preference",
    },
    "procedure": {
        "decay_multiplier": 3.0,   # 3x half-life = slower decay
        "importance_boost": 1.3,
        "recall_priority": 0.75,   # High when task-matched
        "label": "Procedure",
    },
    "mistake": {
        "decay_multiplier": 10.0,  # 10x half-life = very slow decay
        "importance_boost": 2.0,   # 2x importance boost
        "recall_priority": 1.0,    # Highest - always first
        "label": "Mistake",
    },
}

DEFAULT_TYPE = "episodic"

# Severity levels for mistakes
SEVERITY_LEVELS = ("high", "medium", "low")

# -- ingress validation (S1: schema-valid does not mean policy-safe) -----------
# A memory_type is an identifier used in decay/recall math, on-disk labels, and
# surfaced context lines. An externally supplied one that is not a clean slug is
# neutralised to the default (the memory is still stored - never dropped), so a
# hostile "..\x07EVIL" can never reach a filename, a label, or `.title()`.
_MEMORY_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.\-]{0,63}$")

# The ONLY keys a caller-supplied type_config may set, each with a finite numeric
# range (or, for label, a scrubbed short string). Anything else is dropped: an
# unbounded/NaN decay multiplier or a secret smuggled through an arbitrary key
# must never reach the config the decay/recall math and surfaces consume.
_TYPE_CONFIG_NUMERIC_BOUNDS = {
    "decay_multiplier": (0.01, 100.0),
    "importance_boost": (0.0, 100.0),
    "recall_priority": (0.0, 1.0),
}
_TYPE_CONFIG_LABEL_MAX = 64


def valid_memory_type(memory_type: object) -> bool:
    """True iff *memory_type* is a clean lowercase slug safe to use as a type key."""
    return isinstance(memory_type, str) and bool(_MEMORY_TYPE_RE.match(memory_type))


def normalize_memory_type(memory_type: object) -> str:
    """A safe memory_type: the value itself when it is a clean slug, else DEFAULT_TYPE.

    Fail-SAFE, never fail-closed: a hostile or malformed type is neutralised to the
    default so the MEMORY IS STILL STORED (never dropped - the data is what matters);
    only the type identifier is made safe."""
    return memory_type if valid_memory_type(memory_type) else DEFAULT_TYPE


def sanitize_type_config(type_config: Optional[Dict],
                         scrub: Optional[Callable[[str], str]] = None) -> Dict:
    """Reduce a caller-supplied type_config to ONLY the allowlisted keys, with every
    numeric coerced to a finite float clamped into range and the label coerced to a
    short, optionally-scrubbed string. Unknown keys, non-coercible or non-finite
    numerics, and non-string labels are DROPPED (the canonical default then stands).
    This is the one place the config schema lives - every ingress routes through it."""
    if not isinstance(type_config, dict):
        return {}
    safe: Dict = {}
    for key, (lo, hi) in _TYPE_CONFIG_NUMERIC_BOUNDS.items():
        if key not in type_config:
            continue
        try:
            val = float(type_config[key])
        except (TypeError, ValueError):
            continue                      # non-coercible -> drop, default stands
        if not math.isfinite(val):
            continue                      # NaN / +-inf -> drop
        safe[key] = min(hi, max(lo, val))  # clamp finite value into range
    if "label" in type_config:
        label = type_config["label"]
        if isinstance(label, str):
            if scrub is not None:
                label = scrub(label)
            safe["label"] = label[:_TYPE_CONFIG_LABEL_MAX]
    return safe


def get_type_config(memory_type: str, type_config: Optional[Dict] = None) -> Dict:
    """Return the config dict for a memory type.

    If *memory_type* is not in the canonical set and *type_config* is provided,
    the custom config is merged with the episodic defaults and returned.
    Unknown types with no override fall back to episodic defaults.

    Self-defending (defense in depth): the memory_type is normalised and the
    type_config is sanitised HERE too, so even a caller that bypassed the ingress
    gateway cannot push an arbitrary key, an unbounded numeric, or an unscrubbed
    label into the config the decay/recall math and context surfaces read.
    """
    if memory_type in MEMORY_TYPE_CONFIGS:
        return MEMORY_TYPE_CONFIGS[memory_type]

    safe_type = normalize_memory_type(memory_type)
    if safe_type in MEMORY_TYPE_CONFIGS:
        # a hostile/unknown type with no clean slug collapses to the canonical default
        return MEMORY_TYPE_CONFIGS[safe_type]

    # a clean custom slug - merge the sanitised override with the episodic defaults
    base = dict(MEMORY_TYPE_CONFIGS["episodic"])
    base.update(sanitize_type_config(type_config))
    base.setdefault("label", safe_type.title())
    return base


def format_mistake_content(
    what_happened: str,
    correction: str,
    root_cause: Optional[str] = None,
    severity: str = "medium",
) -> str:
    """Format a mistake entry's content string."""
    parts = [
        f"MISTAKE: {what_happened}",
        f"CORRECTION: {correction}",
    ]
    if root_cause:
        parts.append(f"ROOT CAUSE: {root_cause}")
    parts.append(f"SEVERITY: {severity}")
    return " | ".join(parts)


def format_pitfall_line(type_metadata: Dict) -> str:
    """Format the 'Known Pitfall' line for context packets."""
    what = type_metadata.get("what_happened", "unknown issue")
    correction = type_metadata.get("correction", "no correction recorded")
    return f"Known Pitfall: Last time this was attempted, {what}. Correction: {correction}"
