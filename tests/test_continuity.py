"""base-tier - continuity survives compaction on markdown ALONE, engine absent.

A real save -> PreCompact -> SessionStart(fresh process) cycle must bring the working set back,
scrubbed, with no engine involved.
"""

import os
import tempfile

import pytest

from parsica_spirit.continuity import Continuity


def test_working_set_survives_a_precompact_sessionstart_cycle():
    root = tempfile.mkdtemp(prefix="pcx_cont_")
    c = Continuity(root)
    # a working set that also contains a planted secret
    n = c.save_working_set(
        "# Working set\n## Pinned\n- Project: Parsica Spirit\n"
        "- deploy note: api_key=sk-ant-AAAAAAAAAAAAAAAAAAAAAAAA\n"
        "## Open loops\n- [ ] base-tier continuity proof\n"
    )
    assert n >= 1, "the working set is scrubbed on write"
    c.append_journal("Locked P5; building the markdown wedge.", date="2026-06-30")

    # PreCompact: files persist on disk; a boundary marker is dropped
    c.mark_compaction_boundary(date="2026-06-30", stamp="2026-06-30 22:00 UTC")

    # SessionStart in a FRESH process: a new Continuity over the same root, engine absent
    blob = Continuity(root).restore()
    assert "Working set" in blob and "Parsica Spirit" in blob, "the working set must survive compaction"
    assert "base-tier continuity proof" in blob
    assert "Latest journal" in blob and "markdown wedge" in blob
    assert "sk-ant-" not in blob, "a secret must never survive into the re-inject"
    assert "[REDACTED" in blob


def test_unfilled_template_produces_no_reinject():
    root = tempfile.mkdtemp(prefix="pcx_empty_")
    c = Continuity(root)
    c.save_working_set("# Working set\n_(template - nothing filled in yet)_\n")
    assert Continuity(root).restore() == "", "an unfilled template must stay quiet, not echo boilerplate"


def test_scrub_then_slice_has_no_boundary_leak():
    # even with a tiny budget forcing a tail slice, a secret cannot leave a high-entropy remainder
    root = tempfile.mkdtemp(prefix="pcx_slice_")
    c = Continuity(root)
    filler = "detail " * 60
    c.save_working_set("## Pinned\n" + filler + "\napi_key=sk-ant-BBBBBBBBBBBBBBBBBBBBBBBB\n" + filler)
    blob = Continuity(root).restore(budget=300)
    assert "sk-ant-" not in blob


def test_journal_boundary_marker_alone_is_not_real_content():
    root = tempfile.mkdtemp(prefix="pcx_marker_")
    c = Continuity(root)
    c.save_working_set("## Pinned\n- something real\n")
    c.mark_compaction_boundary(date="2026-06-30", stamp="2026-06-30 22:00 UTC")  # only a marker in the journal
    blob = Continuity(root).restore()
    assert "Working set" in blob
    assert "Latest journal" not in blob, "a journal day-file holding only a boundary marker must be skipped"


# --- regression tests for the base-tier adversarial review findings ---

def test_lessons_round_trip_through_restore():
    root = tempfile.mkdtemp(prefix="pcx_les_")
    c = Continuity(root)
    n = c.append_lesson("Lesson: always scrub before store. leaked api_key=sk-ant-AAAAAAAAAAAAAAAAAAAAAAAA")
    assert n >= 1
    blob = Continuity(root).restore()
    assert "Lessons" in blob and "always scrub before store" in blob
    assert "sk-ant-" not in blob


def test_scrub_tail_zero_budget_returns_empty():
    # b[-0:] aliases to b[0:] (the whole file) - a 0 budget must return empty, not pass content
    root = tempfile.mkdtemp(prefix="pcx_zero_")
    c = Continuity(root)
    c.save_working_set("## Pinned\n" + "content " * 200)
    assert c._scrub_tail(c._working, 0) == ("", False)   # (tail, truncated) - 0 budget yields no content


def test_compaction_boundary_marker_is_scrubbed():
    # nothing on the write path reaches disk unscrubbed - including the boundary stamp
    root = tempfile.mkdtemp(prefix="pcx_mark_")
    c = Continuity(root)
    c.mark_compaction_boundary(date="2026-06-30", stamp="deploy api_key=sk-ant-AAAAAAAAAAAAAAAAAAAAAAAA")
    on_disk = open(os.path.join(root, "journal", "2026-06-30.md"), encoding="utf-8").read()
    assert "sk-ant-" not in on_disk and "[REDACTED" in on_disk


def test_journal_date_must_be_iso():
    c = Continuity(tempfile.mkdtemp(prefix="pcx_date_"))
    with pytest.raises(ValueError):
        c.append_journal("entry", date="06/30/2026")
    with pytest.raises(ValueError):
        c.mark_compaction_boundary(date="today")


def test_working_set_tail_slice_is_marked():
    root = tempfile.mkdtemp(prefix="pcx_trunc_")
    c = Continuity(root)
    c.save_working_set("## Pinned\n" + "x" * 5000)
    blob = Continuity(root).restore(budget=600)   # working set forced to a tail-slice
    assert "(earlier content truncated)" in blob, "a silent head-drop must be marked"


# --- round-2 adversarial review fixes ---

def test_boilerplate_only_tail_after_truncation_stays_quiet():
    # real facts live only in the HEAD that gets budgeted out; the surviving tail is pure boilerplate.
    # The truncation marker must NOT resurrect it into a bogus 'Working set' section (round-2 regression).
    root = tempfile.mkdtemp(prefix="pcx_boiler_")
    c = Continuity(root)
    c.save_working_set("- real fact one\n- real fact two\n" + "# heading\n" * 400)
    blob = Continuity(root).restore(budget=600)   # budget//2 -> the surviving tail is only headers
    assert "Working set" not in blob, "a boilerplate-only surviving tail must stay quiet"


def test_restore_nonpositive_budget_returns_empty():
    root = tempfile.mkdtemp(prefix="pcx_negb_")
    c = Continuity(root)
    c.save_working_set("## Pinned\n- real content\n")
    c.append_lesson("a real lesson")
    assert Continuity(root).restore(budget=0) == ""
    assert Continuity(root).restore(budget=-100) == "", "a negative budget must not defeat the cap"


def test_compaction_stamp_cannot_break_out_of_the_comment():
    root = tempfile.mkdtemp(prefix="pcx_inj_")
    c = Continuity(root)
    c.mark_compaction_boundary(date="2026-06-30", stamp="x --> <script>alert(1)</script> <!--")
    on_disk = open(os.path.join(root, "journal", "2026-06-30.md"), encoding="utf-8").read()
    # a caller-supplied stamp cannot inject its own comment delimiters - only the legit pair remains
    assert on_disk.count("-->") == 1 and on_disk.count("<!--") == 1


def test_iso_date_rejects_trailing_newline():
    c = Continuity(tempfile.mkdtemp(prefix="pcx_nl_"))
    with pytest.raises(ValueError):
        c.append_journal("entry", date="2026-06-30\n")   # \Z, not $, closes the newline bypass


def test_recover_is_the_recycle_bin_undelete():
    # the facade's reset-ladder undelete is `recover` now (no collision with continuity.restore)
    from parsica_spirit import open_markdown
    ctx = open_markdown(tempfile.mkdtemp(prefix="pcx_rec_"))
    rid = ctx.store("a memory to forget and recover").id
    assert ctx.forget(rid) and ctx.recover(rid) and ctx.get(rid) is not None


def test_restore_never_exceeds_budget():
    # the load-bearing invariant: the re-inject is ALWAYS within budget, even at absurd tiny budgets
    root = tempfile.mkdtemp(prefix="pcx_bud_")
    c = Continuity(root)
    c.save_working_set("## Pinned\n" + "real fact " * 500)
    c.append_lesson("a real lesson " * 50)
    c.append_journal("a real journal entry " * 50, date="2026-06-30")
    for b in (1, 8, 14, 15, 16, 100, 300, 2000):
        out = Continuity(root).restore(budget=b)
        assert len(out) <= b, "restore(budget=%d) returned %d chars" % (b, len(out))
