"""parsica_spirit.trace - passive routing traces (the sensor seam).

Observational ONLY. Nothing at runtime reads these back. Each
federated read and each promotion appends one trace event plus per-hit exposure rows
to an append-only JSONL side file under the Spirit store.

Hygiene, by construction and then again by gate:
  * content-free fields only - ids, hashes, enums, counts, ranks. The query TEXT is
    ephemeral by contract (users paste secrets into searches): only its hash is
    logged, here and everywhere.
  * every string value still passes the scrub gate (secrets always) before landing -
    belt and suspenders on a surface that should never need it.
  * bytes, never tokens, for any budget field.
  * best-effort: emit() returns None on ANY failure and never raises - a trace must
    never break a read or a promotion. A trace also never scaffolds a store: no
    Spirit root, no trace.

This module is deliberately excluded from the ranking-path wall-clock scan: an
observational timestamp on a log line is not a ranking input.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Dict, List, Optional

from .scrub import scrub as _scrub

TRACE_DIRNAME = ".parsica_router"
TRACE_SUBDIR = "trace"
TRACE_SPEC = "router-trace/v1"


def query_hash(text: str) -> str:
    """The ONLY form of a query that may be persisted."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _clean(v):
    """Deep-scrub string values (secrets always; structure and non-strings verbatim)."""
    if isinstance(v, str):
        s, _n = _scrub(v, scrub_pii=True)
        return s
    if isinstance(v, list):
        return [_clean(x) for x in v]
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    return v


def emit_trace(spirit_root, *, scope_id: str, route_mode: str,
               qhash: Optional[str] = None,
               weave_candidates: int = 0, spirit_candidates: int = 0,
               returned: int = 0, deduped: int = 0,
               layer_order: Optional[List[str]] = None,
               budget_bytes: Optional[int] = None,
               error_kind: Optional[str] = None,
               counts: Optional[Dict[str, int]] = None,
               exposures: Optional[List[dict]] = None) -> Optional[str]:
    """Append one trace event (+ exposure rows) to the store's trace log. Returns the
    event_id, or None on any failure (never raises, never creates the store)."""
    try:
        root = Path(spirit_root)
        if not (root.exists() and root.is_dir()):
            return None
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        event = {
            "kind": "trace", "spec": TRACE_SPEC, "at": at,
            "scope_id": scope_id, "route_mode": route_mode,
            "query_hash": qhash,
            "weave_candidates": int(weave_candidates),
            "spirit_candidates": int(spirit_candidates),
            "returned": int(returned), "deduped": int(deduped),
            "layer_order": list(layer_order or []),
            "budget_bytes": budget_bytes,
            "error_kind": error_kind,
            "counts": dict(counts or {}),
        }
        eid = hashlib.sha256(json.dumps(event, sort_keys=True).encode("utf-8")).hexdigest()[:16]
        event["event_id"] = eid
        rows = [event]
        for i, x in enumerate(exposures or []):
            row = {"kind": "exposure", "event_id": eid, "n": i}
            row.update(x)
            rows.append(row)
        tdir = root / TRACE_DIRNAME / TRACE_SUBDIR
        tdir.mkdir(parents=True, exist_ok=True)
        path = tdir / ("%s.%s.jsonl" % (at[:10], os.getpid()))
        with path.open("a", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(_clean(r), ensure_ascii=False, allow_nan=False) + "\n")
        return eid
    except Exception:
        return None


def exposure(memory_id: str, layer: str, rank: int, *,
             source_kind: Optional[str] = None, content_hash: Optional[str] = None,
             was_deduped: bool = False, was_suppressed: bool = False,
             reason: str = "hit") -> dict:
    """One returned/suppressed memory, content-free."""
    return {"memory_id": memory_id, "layer": layer, "rank": int(rank),
            "source_kind": source_kind, "content_hash": content_hash,
            "was_deduped": bool(was_deduped), "was_suppressed": bool(was_suppressed),
            "reason": reason}
