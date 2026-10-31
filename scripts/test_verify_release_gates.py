#!/usr/bin/env python3
"""Negative tests for the release gates: a gate must fail CLOSED when it cannot actually check.
An enforcer that reports "not checked" as "pass" is worse than one that fails, so these prove
the gates go RED on a missing config, a partial config set, a missing tool, or an empty discovery."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import verify_release as vr


def _write_config(tmp_path):
    (tmp_path / ".importlinter").write_text("[importlinter]\n")


def test_import_gate_fails_without_config(monkeypatch, tmp_path):
    # A release artifact that silently drops .importlinter leaves the zone direction
    # contracts unchecked - must be RED ("cannot certify"), never a lucky green.
    monkeypatch.setattr(vr, "ROOT", str(tmp_path))          # empty dir: config absent
    ok, detail = vr.gate_import_closure()
    assert ok is False and "cannot certify" in detail and ".importlinter" in detail


def test_import_gate_fails_without_executable(monkeypatch, tmp_path):
    # Config present, but the lint-imports tool itself is absent (which() returns None).
    _write_config(tmp_path)
    monkeypatch.setattr(vr, "ROOT", str(tmp_path))
    monkeypatch.setattr(vr.shutil, "which", lambda name: None)
    ok, detail = vr.gate_import_closure()
    assert ok is False and "not installed" in detail


def test_import_gate_fails_on_exec_filenotfound(monkeypatch, tmp_path):
    # Fallback path: which() finds the tool but the exec still raises FileNotFoundError - stay closed.
    _write_config(tmp_path)
    monkeypatch.setattr(vr, "ROOT", str(tmp_path))
    monkeypatch.setattr(vr.shutil, "which", lambda name: "lint-imports")

    def _boom(*a, **k):
        raise FileNotFoundError("lint-imports")

    monkeypatch.setattr(vr, "_run", _boom)
    ok, detail = vr.gate_import_closure()
    assert ok is False and "not installed" in detail


def test_plain_ascii_gate_fails_on_empty_discovery(monkeypatch):
    monkeypatch.setattr(vr, "_shipping_docs", lambda: iter([]))
    ok, detail = vr.gate_plain_ascii_docs()
    assert ok is False and "no shipping" in detail


_STUB_GATES = [
    ("tests", lambda: (True, "stub"), False),
    ("unarmed", lambda: (False, "input absent - would be RED"), False),
    ("licensing", lambda: (True, "stub"), False),
]


def test_an_unknown_flag_skips_nothing(monkeypatch, capsys):
    # --skip-slow is the only flag the verifier knows. Every other flag is ignored, so no
    # invocation can turn a failing gate into a printed SKIP and an exit code of 0; until October
    # 2026 one flag did exactly that, and CI always passed it (REVIEW-2026-10.md, pile one, class
    # (e)).
    monkeypatch.setattr(vr, "GATES", _STUB_GATES)
    monkeypatch.setattr(vr.sys, "argv", ["verify_release.py", "--skip-unarmed", "--skip-closure-armed"])
    rc = vr.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "SKIP" not in out
    assert "FAIL   unarmed" in out and "RED" in out
    assert out.count("PASS") == 2                      # the other two gates still ran


def test_every_gate_runs_by_default(monkeypatch, capsys):
    # the DEFAULT stays fail-closed: a gate that reports its input missing is RED, never skipped.
    monkeypatch.setattr(vr, "GATES", _STUB_GATES)
    monkeypatch.setattr(vr.sys, "argv", ["verify_release.py"])
    rc = vr.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "SKIP" not in out
    assert "FAIL   unarmed" in out and "RED" in out


def test_the_excluded_module_closure_gate_is_retired():
    # The excluded-module closure gate was retired in October 2026 (REVIEW-2026-10.md, follow-up,
    # class (e)): its two test modules are gone, the verifier has no arming gate and reads no
    # sidecar list, and the remaining gates are exactly these five, every one of which runs.
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel in ("tests/engine/test_closure.py", "tests/engine/test_closure_gate_fails_closed.py"):
        assert not os.path.exists(os.path.join(root, rel)), "%s is back in the tree" % rel
    assert [name for name, _, _ in vr.GATES] == ["tests", "import-closure", "plain-ascii-docs",
                                                 "clean-room", "licensing"]
    src = open(vr.__file__, encoding="utf-8").read()
    assert "gate_closure_armed" not in src and "closure_excluded" not in src
    assert "skip_closure" not in src
