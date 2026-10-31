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
    ("closure-armed", lambda: (False, "sidecar absent - would be RED"), False),
    ("licensing", lambda: (True, "stub"), False),
]


def test_the_old_skip_flag_no_longer_skips_the_closure_gate(monkeypatch, capsys):
    # Until October 2026 --skip-closure-armed turned a missing gate input into a printed SKIP and
    # an exit code of 0, and CI always passed it (REVIEW-2026-10.md, pile one, class (e)). The
    # flag is gone: an old invocation runs every gate, and an unarmed closure gate is RED.
    monkeypatch.setattr(vr, "GATES", _STUB_GATES)
    monkeypatch.setattr(vr.sys, "argv", ["verify_release.py", "--skip-closure-armed"])
    rc = vr.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "SKIP" not in out
    assert "FAIL   closure-armed" in out and "RED" in out
    assert out.count("PASS") == 2                      # the other two gates still ran


def test_closure_armed_fails_without_the_list_even_outside_a_git_checkout(monkeypatch, tmp_path):
    # Until October 2026 a tree with no .git directory passed this gate with the list absent
    # (verify_release.py 101-105). A gate with no input fails everywhere.
    monkeypatch.setattr(vr, "ROOT", str(tmp_path))          # no .git, no sidecar
    ok, detail = vr.gate_closure_armed()
    assert ok is False and "absent" in detail and "fails" in detail
    sidecar = tmp_path / "tests" / "engine" / "closure_excluded.local.txt"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text("# only a comment\n", encoding="utf-8")
    ok, detail = vr.gate_closure_armed()
    assert ok is False and "empty" in detail
    sidecar.write_text("zz_some_private_module\n", encoding="utf-8")
    ok, detail = vr.gate_closure_armed()
    assert ok is True and "1 excluded" in detail and "zz_some_private_module" not in detail


def test_closure_armed_runs_by_default(monkeypatch, capsys):
    # the DEFAULT stays fail-closed: without the flag, an unarmed closure gate is RED.
    monkeypatch.setattr(vr, "GATES", _STUB_GATES)
    monkeypatch.setattr(vr.sys, "argv", ["verify_release.py"])
    rc = vr.main()
    out = capsys.readouterr().out
    assert rc == 1
    assert "FAIL   closure-armed" in out and "RED" in out
