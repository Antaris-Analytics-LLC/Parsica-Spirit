"""Session isolation (session_awareness): trigger-based multi-session write gate.

Three modes:
"off" = no gate (default, full back-compat); "open" = permissive, idle writes
logged; "closed" = strict - only sessions activated via begin_turn() may write.
Isolation is for writes, not reads: recall is governed separately by
cross_session_recall.
"""
import tempfile

import pytest

from parsica_spirit.engine import MemorySystem

NOTE = "The deployment checklist requires a two-step verification gate"


def _mem(**kw):
    return MemorySystem(tempfile.mkdtemp(prefix="sa_"), agent_name="tester", **kw)


def test_off_mode_default_has_no_gate():
    mem = _mem()
    assert mem.ingest(NOTE, session_id="s1") >= 1
    # lifecycle calls are safe no-ops when off
    mem.begin_turn("s1")
    mem.end_turn("s1")


def test_closed_blocks_unregistered_then_allows_active_then_blocks_idle():
    mem = _mem(session_awareness="closed")
    # cold start, never activated -> blocked, store untouched
    assert mem.ingest(NOTE, session_id="s1") == 0
    assert len(mem.memories) == 0
    # activate -> allowed
    mem.begin_turn("s1")
    assert mem.ingest(NOTE, session_id="s1") >= 1
    # idle again -> blocked
    mem.end_turn("s1")
    assert mem.ingest("A different note about the release gate steps",
                      session_id="s1") == 0


def test_closed_blocks_anonymous_writes_when_sessions_tracked():
    mem = _mem(session_awareness="closed")
    mem.begin_turn("s1")
    assert mem.ingest(NOTE, session_id=None) == 0


def test_closed_pending_task_keeps_session_writable():
    mem = _mem(session_awareness="closed")
    mem.begin_turn("s1")
    task = mem.add_task("s1")
    assert task, "add_task must return a usable task name"
    mem.end_turn("s1")  # tasks still pending -> session stays ACTIVE
    assert mem.ingest(NOTE, session_id="s1") >= 1
    mem.complete_task("s1", task)  # last task done + turn ended -> IDLE
    assert mem.ingest("Another note after the tasks completed fully",
                      session_id="s1") == 0


def test_open_mode_allows_unregistered_writes():
    mem = _mem(session_awareness="open")
    assert mem.ingest(NOTE, session_id="s1") >= 1


def test_invalid_value_warns_loudly_and_falls_back_to_off():
    # the exact typo the loud-fail exists for: "closd" must not silently
    # leave the user thinking they have isolation when they don't
    with pytest.warns(UserWarning, match="session_awareness"):
        mem = _mem(session_awareness="closd")
    assert mem.ingest(NOTE, session_id="s1") >= 1  # gate is off


def test_closed_gates_typed_ingest_and_mistakes():
    mem = _mem(session_awareness="closed")
    mem.begin_turn("s1")  # sessions are now tracked
    assert mem.ingest_fact("The staging database lives in region us-east-2",
                           session_id="s2") == 0
    assert mem.ingest_mistake("Assumed the cache was warm; it was not.",
                              "Verify cache state before relying on it.",
                              session_id="s2") is None
    mem.begin_turn("s2")
    assert mem.ingest_fact("The staging database lives in region us-east-2",
                           session_id="s2") >= 1


def test_closed_gates_forget():
    mem = _mem(session_awareness="closed")
    mem.begin_turn("s1")
    assert mem.ingest(NOTE, session_id="s1") >= 1
    n = len(mem.memories)
    mem.end_turn("s1")
    # idle session cannot forget (forget mutates the store)
    res = mem.forget(topic="deployment", session_id="s1")
    assert res["removed"] == [] and len(mem.memories) == n
    # active session can
    mem.begin_turn("s1")
    res = mem.forget(topic="deployment", session_id="s1")
    assert res["removed"]


def test_force_idle_escape_hatch():
    mem = _mem(session_awareness="closed")
    mem.begin_turn("s1")
    mem.add_task("s1")  # leaked task would keep the session ACTIVE forever
    mem.force_idle_session("s1")
    assert mem.ingest(NOTE, session_id="s1") == 0
