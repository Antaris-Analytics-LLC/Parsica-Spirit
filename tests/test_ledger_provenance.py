"""BLOCKER 3 regression: the promotion ledger slot must be per-project distinct.

The old ledger_key was `weave:<scope>:<source_kind>:<source_record_id>` - it left
source_project OUT, so two projects that promote the SAME source_record_id into one
scope (the shared-doc case Weave's workspace layout makes routine) collided onto a
single ledger slot: the second promote's mark_active silently overwrote the first, and
verify then reported the first record as unledgered. The fix keys the slot on
scope_id + provenance_key (a 16-hex digest that already folds source_project in), so the
two take distinct slots.
"""
import os
import stat
import tempfile

import pytest

from parsica_spirit import open_markdown
from parsica_spirit.promote import (
    PromotionCandidate, SyncLedger, apply_promotion, plan_promotion, verify_ledger,
)
from parsica_spirit.router import RouterScope

SCOPE = RouterScope(scope_id="shared-scope-abcdef012345")


def _cand(record_id, project, body="never push unreviewed", kind="lesson"):
    return PromotionCandidate(scope_id=SCOPE.scope_id, source_project=project,
                              source_kind=kind, source_record_id=record_id, body=body)


def _spirit():
    root = tempfile.mkdtemp(prefix="pr_ledprov_")
    ctx = open_markdown(root)
    ctx.scope_identity("test-workspace")
    return root, ctx


def test_same_record_id_different_project_get_distinct_keys():
    """(a) identical scope_id + source_kind + source_record_id, DIFFERENT
    source_project: provenance_key differs AND ledger_key differs (no collision)."""
    a = _cand("lesson:2026-07-01:0:aa11bb22", project="projA-1111111111")
    b = _cand("lesson:2026-07-01:0:aa11bb22", project="projB-2222222222")
    assert a.scope_id == b.scope_id
    assert a.source_kind == b.source_kind
    assert a.source_record_id == b.source_record_id
    assert a.provenance_key != b.provenance_key, "provenance folds source_project in"
    assert a.ledger_key != b.ledger_key, "distinct sources must not share a ledger slot"


def test_promoting_two_projects_same_source_id_leaves_no_drift():
    """(b) promote both into ONE scope; verify_ledger reports no drift. Under the old
    single-key shape the two collided onto one slot and verify flagged the first
    record as unledgered."""
    root, ctx = _spirit()
    ledger = SyncLedger(root, SCOPE.scope_id)
    a = _cand("lesson:2026-07-01:0:aa11bb22", project="projA-1111111111")
    b = _cand("lesson:2026-07-01:0:aa11bb22", project="projB-2222222222")
    plan = plan_promotion(ctx, SCOPE, [a, b], ledger)
    assert [x.kind for x in plan] == ["store", "store"]
    counts = apply_promotion(ctx, plan, ledger)
    assert counts["store"] == 2 and counts["error"] == 0
    # two live records, two ledger slots, one for each provenance
    ctx2 = open_markdown(root)
    fresh = SyncLedger(root, SCOPE.scope_id)
    assert {a.ledger_key, b.ledger_key} <= set(fresh.active_keys())
    assert verify_ledger(ctx2, SCOPE, fresh) == []


def test_ledger_file_is_owner_only_on_posix():
    """The sync ledger holds pks, spirit ids, and hashes; save() writes it owner-only
    (0600, temp AND final). POSIX-only: Windows NTFS ACLs already scope to the owner
    and os.chmod is a no-op there, so this perms assertion skips - mirrors the symlink
    test's platform guard."""
    if not hasattr(os, "geteuid"):
        pytest.skip("POSIX permission bits not applicable on this platform")
    root, ctx = _spirit()
    ledger = SyncLedger(root, SCOPE.scope_id)
    c = _cand("lesson:2026-07-01:0:aa11bb22", project="projA-1111111111")
    apply_promotion(ctx, plan_promotion(ctx, SCOPE, [c], ledger), ledger)
    path = os.path.join(root, ".parsica_router", "promotion_sync.json")
    assert os.path.exists(path)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, "ledger must be owner-only, got %o" % mode
