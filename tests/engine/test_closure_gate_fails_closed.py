"""The import-closure gate fails when its input is missing. REVIEW-2026-10.md, pile one, class (e).

Until October 2026 tests/engine/test_closure.py skipped every test when its exclusion list
(closure_excluded.local.txt, kept out of the repository) was absent, and verify_release.py
accepted a flag that turned the same absence into a skip, which CI always passed. These tests run
the closure module in a subprocess against three sidecar states and assert the gate's three
honest answers: absent -> FAIL by name, empty -> FAIL, present with a name -> the closure tests
RUN and pass on this clean tree.

The closure module is copied next to a sidecar in a temporary directory so the repository's own
tree is never written to; it imports parsica_spirit.engine and scans the installed package, so
it works from any location.
"""
import os
import pathlib
import shutil
import subprocess
import sys

HERE = pathlib.Path(__file__).parent
CLOSURE = HERE / "test_closure.py"


def _run_closure(tmp_path, sidecar_text):
    work = tmp_path / "closure_run"
    work.mkdir()
    shutil.copy(str(CLOSURE), str(work / "test_closure.py"))
    if sidecar_text is not None:
        (work / "closure_excluded.local.txt").write_text(sidecar_text, encoding="utf-8")
    env = dict(os.environ)
    src = str(HERE.parent.parent / "src")
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    p = subprocess.run([sys.executable, "-m", "pytest", str(work / "test_closure.py"), "-q",
                        "-p", "no:cacheprovider"], cwd=str(work), env=env,
                       capture_output=True, text=True, timeout=300)
    return p.returncode, p.stdout + p.stderr


def test_absent_list_fails_by_name_and_never_skips(tmp_path):
    rc, out = _run_closure(tmp_path, None)
    assert rc != 0, out[-800:]
    assert "skipped" not in out, "an absent input must not be reported as a skip:\n" + out[-800:]
    assert "exclusion list absent" in out and "does not pass" in out, out[-800:]


def test_empty_list_fails(tmp_path):
    rc, out = _run_closure(tmp_path, "# nothing but a comment\n")
    assert rc != 0 and "skipped" not in out, out[-800:]
    assert "exclusion list is empty" in out, out[-800:]


def test_present_list_arms_the_gate_and_it_passes_on_this_tree(tmp_path):
    rc, out = _run_closure(tmp_path, "zz_private_module_that_must_not_ship\n")
    assert rc == 0, out[-1200:]
    assert "passed" in out and "skipped" not in out, out[-800:]


def test_closure_module_declares_no_skip_marker():
    src = CLOSURE.read_text(encoding="utf-8")
    assert "skipif" not in src and "pytestmark" not in src, "the closure gate must fail, never skip"
