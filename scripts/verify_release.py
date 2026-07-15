#!/usr/bin/env python3
"""Release check - one command that says whether Parsica Spirit is releasable.

Composes every gate we already built into a single PASS/FAIL verdict, so the repo lands green and
there is one honest signal before external review and publication. It publishes NOTHING;
publication waits on the live sites and the joint test pass.

Gates:
  - tests            - full pytest suite (facade + engine + leaf + scripts; src/ is bootstrapped
                       onto the child PYTHONPATH, so a bare checkout needs nothing installed)
  - import-closure   - import-linter zone contracts (.importlinter); fails closed if the
                       required config or the tool is absent
  - closure-armed    - the closure-test exclusion sidecar must be present and non-empty in a repo
                       checkout (fails closed; an unpacked public artifact reports a site-local
                       skip instead). --skip-closure-armed omits it for public CI, where the
                       sidecar never exists by design; the armed run is the release-side ritual
  - plain-ascii-docs - shipped .md docs carry no AI-tell glyphs (em/en dash, ellipsis, curly quotes,
                       bullet, arrows, box-art, emoji); fails on an empty discovery, never certifies nothing
  - clean-room       - one wheel, inventory closed, zero declared dependencies, base lifecycle
                       runs engine-dark, engine tier runs (scripts/cleanroom_check.py)
  - licensing        - Apache-2.0 LICENSE + NOTICE at the root, grant declared in pyproject

Exit 0 iff every gate RUN passes. --skip-slow skips the slow gates while iterating;
--skip-closure-armed skips only the sidecar arming check (public CI use).
"""

import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Child env for every gate subprocess: force UTF-8 I/O so capture never chokes on a stray output byte
# re-exec itself - os.execv on Windows muddies the exit code and output of a captured subprocess, which
# once made this gate print a lucky "PASS" over a traceback. A release gate must never do that.
# Fresh-checkout bootstrap: put src/ on the child PYTHONPATH so the gate runs from an
# unzipped tree with nothing installed (an editable install also works, and wins).
_SRC = os.path.join(ROOT, "src")
_PYPATH = _SRC + ((os.pathsep + os.environ["PYTHONPATH"]) if os.environ.get("PYTHONPATH") else "")
_CHILD_ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONHASHSEED": "0",
              "PYTHONPATH": _PYPATH}


def _run(cmd, cwd=ROOT):
    # decode utf-8 with errors='replace': the gate must never crash on a subprocess's output bytes.
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, encoding="utf-8", errors="replace", env=_CHILD_ENV)
    return p.returncode == 0, (p.stdout or "") + (p.stderr or "")


def gate_tests():
    ok, out = _run([sys.executable, "-m", "pytest", "tests", "scripts", "-q"])
    return ok, "\n      ".join(out.strip().splitlines()[-2:])


def gate_import_closure():
    # Fail closed: a release verifier must never translate "not checked" into "pass". The
    # config is REQUIRED: a release artifact that silently drops .importlinter would leave the
    # zone direction contracts unchecked - that is RED, not a lucky green. A missing tool is
    # RED for the same reason. (tests/test_seam_guards.py keeps the config's enumerated module
    # lists in sync with the discovered source tree, so the contracts cannot go quietly stale.)
    required = (".importlinter",)
    missing = [c for c in required if not os.path.exists(os.path.join(ROOT, c))]
    if missing:
        return False, ("missing required import-linter configuration(s): " + ", ".join(missing)
                       + " - cannot certify import closure")
    if shutil.which("lint-imports") is None:
        return False, ("import-linter is required for release certification but is not installed; "
                       "install the release-verification dependencies (pip install import-linter)")
    details = []
    for cfg in required:
        try:
            ok, out = _run(["lint-imports", "--config", cfg])
        except FileNotFoundError:
            # Fallback: which() found it but the exec still failed - stay closed, do not certify.
            return False, ("import-linter is required for release certification but is not installed; "
                           "install the release-verification dependencies (pip install import-linter)")
        lines = out.strip().splitlines()
        details.append("%s: %s" % (cfg, "ok" if ok else (lines[-1] if lines else "FAIL")))
        if not ok:
            return False, " | ".join(details)
    return True, " | ".join(details)


def gate_closure_armed():
    """Fail closed if the import-closure exclusion gate would run UNARMED at release time.

    The exclusion vocabulary is a gitignored, site-local sidecar - the names are themselves a tell,
    so they never ship, and the public closure tests SKIP VISIBLY without it. But a RELEASE must be
    cut where the sidecar is present and non-empty, or the gate scanned nothing. So: in a repo
    checkout a missing/empty sidecar is RED (never cut a release unarmed); in an unpacked public
    artifact (no .git) the sidecar is legitimately absent by design, reported as a site-local skip
    rather than a false alarm. Reports the COUNT only - never a name."""
    sidecar = os.path.join(ROOT, "tests", "engine", "closure_excluded.local.txt")
    if os.path.exists(sidecar):
        names = [ln.strip() for ln in open(sidecar, encoding="utf-8").read().splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        if not names:
            return False, "closure exclusion sidecar is empty - the gate would run UNARMED"
        return True, "closure gate armed (%d excluded module(s); names withheld - site-local)" % len(names)
    if os.path.isdir(os.path.join(ROOT, ".git")):
        return False, ("closure exclusion sidecar absent in a repo checkout - the gate would run "
                       "UNARMED; restore tests/engine/closure_excluded.local.txt "
                       "before cutting a release")
    return True, "closure vocabulary is site-local and not shipped (armed release-side; tests skip here)"


def gate_cleanroom():
    ok, out = _run([sys.executable, os.path.join("scripts", "cleanroom_check.py")])
    return ok, "\n      ".join(out.strip().splitlines()[-2:])


def gate_licensing():
    problems = []
    for f in ("LICENSE", "NOTICE"):
        if not os.path.exists(os.path.join(ROOT, f)):
            problems.append("missing root %s (the wheel would not carry the grant)" % f)
    lic = os.path.join(ROOT, "LICENSE")
    if os.path.exists(lic):
        txt = open(lic, encoding="utf-8").read()
        if "Apache License" not in txt or "Version 2.0" not in txt:
            problems.append("root LICENSE is not the Apache-2.0 text")
    pj = os.path.join(ROOT, "pyproject.toml")
    toml = open(pj, encoding="utf-8").read() if os.path.exists(pj) else ""
    if 'license = "Apache-2.0"' not in toml:
        problems.append('pyproject missing license = "Apache-2.0"')
    if "license-files" not in toml:
        problems.append("pyproject missing license-files")
    if problems:
        return False, "; ".join(problems)
    return True, "Apache-2.0 (root LICENSE + NOTICE; grant declared in pyproject)"


# --- plain-ASCII doc lint -----------------------------------------------------
# AI-tell presentation glyphs that must not appear in shipped prose. The denylist is by
# \u escape (not literal glyphs) so the rule is auditable and tamper-evident. Functional
# non-ASCII is deliberately NOT flagged: the middle-dot U+00B7 (byline / migrated-stamp
# separator), accented or CJK characters in multilingual examples, and data-file emoji all
# pass. This scans DOCS only, and only for these tells, so canon stops depending on a human
# noticing an em dash.
_AI_TELLS = {
    "\u2014": "em dash", "\u2013": "en dash", "\u2015": "horizontal bar", "\u2012": "figure dash",
    "\u2026": "ellipsis", "\u2022": "bullet",
    "\u2018": "curly quote", "\u2019": "curly quote", "\u201c": "curly quote", "\u201d": "curly quote",
    "\u00a0": "no-break space",
}
_DOC_SKIP_PREFIX = ("UPSTREAM_",)   # vendored upstream docs keep their upstream text


def _ascii_tell(ch):
    o = ord(ch)
    if ch in _AI_TELLS:
        return _AI_TELLS[ch]
    if (0x2190 <= o <= 0x21FF) or (0x27F0 <= o <= 0x27FF) or (0x2900 <= o <= 0x297F):
        return "arrow"
    if 0x2500 <= o <= 0x257F:
        return "box-drawing"
    if 0x2300 <= o <= 0x23FF:
        return "misc-technical"
    if (0x2600 <= o <= 0x27BF) or (0x2B00 <= o <= 0x2BFF) or \
       (0x1F000 <= o <= 0x1FAFF) or (0x1F1E6 <= o <= 0x1F1FF):
        return "emoji"
    return None


def _git_tracked_md():
    """Tracked .md via git, or None if git is unavailable / this is not a repo."""
    try:
        r = subprocess.run(["git", "-c", "core.quotePath=false", "ls-files", "*.md"],
                           cwd=ROOT, capture_output=True, encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return None
    if r.returncode != 0:
        return None
    return [ln for ln in r.stdout.splitlines() if ln.endswith(".md")]


def _walk_md():
    out = []
    for dp, dns, fns in os.walk(ROOT):
        dns[:] = [d for d in dns if not d.startswith(".") and d != "node_modules"]
        for f in fns:
            if f.endswith(".md"):
                out.append(os.path.relpath(os.path.join(dp, f), ROOT))
    return out


def _shipping_docs():
    # Discover the tracked doc set so a doc is gated the moment it is added (before it lands on
    # public). Fall back to a filesystem walk when git is unavailable - a non-git sdist / ZIP is
    # still scanned, never vacuously passed. Dev-only docs skip via .local. / UPSTREAM_
    # conventions (BUILD_LOG stays untracked; design docs are *.local.md).
    rels = _git_tracked_md()
    if rels is None:
        rels = _walk_md()
    for rel in sorted(set(rels)):
        base = os.path.basename(rel)
        if ".local." in base or base.startswith(_DOC_SKIP_PREFIX):
            continue
        p = os.path.join(ROOT, rel)
        if os.path.isfile(p):
            yield p


def gate_plain_ascii_docs():
    # self-test: the scanner must catch the common tells (em dash, bullet, star) and must NOT
    # flag the functional middle-dot, or the gate is toothless.
    if not (_ascii_tell("\u2014") and _ascii_tell("\u2022") and _ascii_tell("\u2b50")) or _ascii_tell("\u00b7"):
        return False, "SELF-TEST FAILED: scanner coverage is wrong"
    docs = list(_shipping_docs())
    if not docs:
        return False, "no shipping .md docs discovered (not a git checkout, or git unavailable) - cannot certify plain ASCII"
    hits = []
    for path in docs:
        try:
            lines = open(path, encoding="utf-8").read().splitlines()
        except Exception as e:
            hits.append("%s: unreadable (%s)" % (os.path.relpath(path, ROOT), e))
            continue
        for ln, line in enumerate(lines, 1):
            for ch in line:
                kind = _ascii_tell(ch)
                if kind:
                    hits.append("%s:%d %s (U+%04X)" % (os.path.relpath(path, ROOT), ln, kind, ord(ch)))
                    break
    if hits:
        shown = "; ".join(hits[:6]) + (" ..." if len(hits) > 6 else "")
        return False, "%d line(s) with AI-tell glyphs across shipped docs: %s" % (len(hits), shown)
    return True, "plain ASCII across %d shipped .md docs (no AI-tell glyphs)" % len(docs)


GATES = [
    ("tests", gate_tests, False),
    ("import-closure", gate_import_closure, False),
    ("closure-armed", gate_closure_armed, False),
    ("plain-ascii-docs", gate_plain_ascii_docs, False),
    ("clean-room", gate_cleanroom, True),
    ("licensing", gate_licensing, False),
]


def main():
    skip_slow = "--skip-slow" in sys.argv
    skip_closure = "--skip-closure-armed" in sys.argv
    results = []
    skipped = []
    for name, fn, slow in GATES:
        if slow and skip_slow:
            print("SKIP   %-15s (--skip-slow)" % name)
            skipped.append(name)
            continue
        if name == "closure-armed" and skip_closure:
            # public CI: the exclusion sidecar is site-local by design and never exists on a
            # public runner, so the gate would be permanently red there for the wrong reason.
            # The skip is explicit and printed; the DEFAULT stays fail-closed, and the armed
            # run remains the release-side ritual.
            print("SKIP   %-15s (--skip-closure-armed: sidecar is site-local, never on a "
                  "public runner; armed run is release-side)" % name)
            skipped.append(name)
            continue
        print("RUN    %-15s ..." % name)
        try:
            ok, detail = fn()
        except Exception as e:                                   # a gate must never crash the harness
            ok, detail = False, "gate raised %s: %s" % (type(e).__name__, e)
        results.append((name, ok, detail))
        print("%-6s %-15s %s" % ("PASS" if ok else "FAIL", name, detail))
    print("\n" + "=" * 72)
    failed = [n for n, ok, _ in results if not ok]
    if failed:
        print("RELEASE CHECK: RED  - %d gate(s) failed: %s" % (len(failed), ", ".join(failed)))
        return 1
    if skipped:
        print("RELEASE CHECK: PARTIAL - every gate run is green (skipped: %s)" % ", ".join(skipped))
        return 0
    print("RELEASE CHECK: GREEN - Parsica Spirit passes every buildable gate.")
    print("(the release trigger stays parked on the live sites and the joint test pass.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
