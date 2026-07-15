"""Shared helpers for the reference backends: tokenizer, content-id, label match, PlanLedger.

Unicode-aware tokenization (\\w+ with casefold) so non-ASCII content is retrievable and two
backends can't 'conform' yet return different results for accented/CJK text.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Iterable, Mapping, Optional

from .storage import DISPOSITIONS, PlanError, ResetPlan, StoredRecord

_WORD = re.compile(r"\w+", re.UNICODE)


def tokens(text: str) -> set:
    return {w.casefold() for w in _WORD.findall(text or "")}


def searchable(r: StoredRecord) -> str:
    parts = [r.content]
    e = r.enrichment
    if e is not None:
        parts.append(e.summary)
        parts.extend(e.tags)
        parts.extend(e.keywords)
        parts.extend(e.search_queries)
    return " ".join(parts)


def content_id(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()[:16]


def labels_match(record_labels: Mapping[str, str], flt: Optional[Mapping[str, str]]) -> bool:
    if not flt:
        return True
    return all(record_labels.get(k) == v for k, v in flt.items())


class PlanLedger:
    """Enforces plan -> confirm -> execute.

    A plan is a snapshot: consumed exactly once, and ANY mutation invalidates every pending
    plan (so a destructive op can never run against a store that changed after it was
    previewed). A per-instance salt makes plan hashes unique across ledger instances.
    """

    def __init__(self) -> None:
        self._pending: dict = {}
        self._counter = 0
        self._salt = secrets.token_hex(8)  # per-instance: plan hashes never collide across instances

    def plan(self, *, scope: str, disposition: str, affected_ids: Iterable[str]) -> ResetPlan:
        if disposition not in DISPOSITIONS:
            raise PlanError("unknown disposition: {!r}".format(disposition))
        affected = frozenset(affected_ids)
        self._counter += 1
        src = "{}:{}:{}:{}:{}".format(self._salt, self._counter, scope, disposition, "|".join(sorted(affected)))
        plan_hash = hashlib.sha256(src.encode("utf-8")).hexdigest()[:12]
        self._pending[plan_hash] = (scope, disposition, affected)
        return ResetPlan(plan_hash=plan_hash, scope=scope, disposition=disposition, affected=len(affected))

    def verify_and_consume(self, plan_hash: str):
        """Consume a pending plan exactly once. The CALLER then re-checks the affected set against
        the plan's own scope and invalidates the remaining plans (a reset is itself a store
        mutation), so a stale or superseded plan can never execute."""
        entry = self._pending.pop(plan_hash, None)
        if entry is None:
            raise PlanError("unknown or already-consumed plan_hash")
        return entry  # (scope, disposition, frozenset(affected))

    def invalidate(self) -> None:
        self._pending.clear()
