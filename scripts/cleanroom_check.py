#!/usr/bin/env python3
"""Clean-room check - the shipped artifact, from a freshly built wheel in a fresh venv.

Parsica Spirit ships as ONE wheel with ZERO runtime dependencies. Three stages:

STAGE 1 (artifact closure): build the single wheel and verify its inventory - exactly one
top-level import package (parsica_spirit); the engine and contracts subpackages PRESENT
(the one-install promise is structural, not metadata); no legacy top-level module name
(parsica_memory / parsica_enrich - the pre-fold names must never reappear); and the wheel
METADATA declares no unconditional Requires-Dist at all.

STAGE 2 (dependency inversion, engine dark): install the wheel into a fresh venv with
--no-index (a sneaky runtime dependency would fail resolution right here) and run the full
base-tier lifecycle (scrub + query + continuity + identity) under a meta-path blocker that
makes ANY import of parsica_spirit.engine raise. The blocker self-tests first - a
deliberate engine import must fail - so the guard can never rot into a no-op. This is the
repeatable artifact for the "markdown wedge stands alone" architecture guarantee: the
engine ships in the same wheel, but the base tier's code never touches it.

STAGE 3 (engine tier): the same install, no blocker - the engine backend escalates the
same facade and the engine-tier lifecycle runs end to end.

Exit 0 iff all stages pass, 1 on any failure.
"""

import glob
import os
import subprocess
import sys
import tempfile
import venv
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The pre-fold distribution names. They must never reappear as a path component in the
# shipped wheel - as a top-level module, a package directory, or a nested subpackage.
EXCLUDED_STEMS = {"parsica_memory", "parsica_enrich"}

# The ONLY top-level import package the wheel may ship - an allowlist is leak-proof by
# construction: any foreign top-level package is flagged no matter what it is named.
ALLOWED_TOP = {"parsica_spirit"}

# The one-install promise, structurally: the wheel MUST carry the facade, the engine
# subpackage, and the contracts subpackage. A wheel missing any of these is not the product.
REQUIRED_MEMBERS = (
    "parsica_spirit/__init__.py",
    "parsica_spirit/engine/__init__.py",
    "parsica_spirit/enrich/__init__.py",
)


def _closure_leaks(names):
    """Return the closure violations in a wheel's namelist. Three independent gates:
      * DENYLIST - any path COMPONENT (directory or file stem) equal to a legacy module name.
        Checking EVERY component (not just the leaf basename) catches a package-directory
        leak such as `parsica_memory/manager.py` or `parsica_spirit/parsica_memory/__init__.py`.
      * ALLOWLIST - the top-level import package of every module must be in ALLOWED_TOP; this
        catches a whole foreign package even though no denylist enumerates it.
      * REQUIRED - every member of REQUIRED_MEMBERS must be present (one wheel = whole product).
    `.dist-info` metadata is ignored; a `.data` tree is ignored EXCEPT purelib/platlib, which
    install as importable code and are re-rooted at their installed path before the gates run."""
    leaks = []
    seen = set()
    for name in names:
        parts = name.replace("\\", "/").strip("/").split("/")
        if not parts or not parts[0]:
            continue
        top = parts[0]
        if top.endswith(".dist-info"):
            continue
        if top.endswith(".data"):
            if len(parts) >= 3 and parts[1] in ("purelib", "platlib"):
                parts = parts[2:]                       # re-root at the installed package path
            else:
                continue                                # scripts/headers/data are not importable code
            if not parts or not parts[0]:
                continue
            top = parts[0]
        seen.add("/".join(parts))
        stems = {p[:-3] if p.endswith(".py") else p for p in parts}
        hit = stems & EXCLUDED_STEMS
        if hit:
            leaks.append("%s (legacy module name %s)" % (name, sorted(hit)))
            continue
        top_pkg = top[:-3] if top.endswith(".py") else top
        if top_pkg and top_pkg not in ALLOWED_TOP:
            leaks.append("%s (non-allowlisted top-level %r)" % (name, top_pkg))
    for req in REQUIRED_MEMBERS:
        if req not in seen:
            leaks.append("MISSING required member %s (the wheel is not the whole product)" % req)
    return leaks


def _selftest_closure():
    """Negative controls, run every invocation so the gate can never silently rot into a no-op:
    it MUST catch a legacy name as a flat module, a package directory, a nested subpackage, and
    one hidden in a .data/purelib tree; MUST catch a foreign top-level and a missing required
    member; and MUST pass a clean product wheel."""
    clean = list(REQUIRED_MEMBERS) + [
        "parsica_spirit/scrub.py", "parsica_spirit/backends/markdown.py",
        "parsica_spirit/engine/core.py", "parsica_spirit/enrich/record.py",
        "parsica_spirit-1.0.dist-info/RECORD"]
    assert _closure_leaks(clean) == [], "clean product wheel must not be flagged: %r" % _closure_leaks(clean)
    for leak in (["parsica_memory.py"],
                 ["parsica_memory/manager.py"],
                 ["parsica_spirit/parsica_memory/__init__.py"],
                 ["parsica_enrich/__init__.py"],
                 ["some_other_pkg/x.py"],
                 ["parsica_spirit-1.0.data/purelib/parsica_memory/wal.py"]):
        assert _closure_leaks(list(REQUIRED_MEMBERS) + leak), \
            "closure gate FAILED to catch a leak: %r" % leak
    missing = [m for m in _closure_leaks(list(REQUIRED_MEMBERS)[:-1]) if "MISSING required" in m]
    assert missing, "closure gate FAILED to catch a missing required member"


def _metadata_deps(whl):
    """Unconditional Requires-Dist lines in the wheel METADATA (an extra-marked line, e.g. the
    test extra, is conditional and allowed; a bare one is a runtime dependency and a failure)."""
    bad = []
    with zipfile.ZipFile(whl) as z:
        meta_names = [n for n in z.namelist() if n.endswith(".dist-info/METADATA")]
        for mn in meta_names:
            for line in z.read(mn).decode("utf-8", "replace").splitlines():
                if line.startswith("Requires-Dist:") and "extra ==" not in line:
                    bad.append(line.strip())
    return bad


SMOKE = r'''
import sys, tempfile

# Meta-path blocker: the base-tier lifecycle below must run with the engine UNIMPORTABLE.
class _EngineBlocker:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "parsica_spirit.engine" or fullname.startswith("parsica_spirit.engine."):
            raise ImportError("BLOCKED: the base tier must never import parsica_spirit.engine")
        return None

sys.meta_path.insert(0, _EngineBlocker())

# negative control: the blocker must actually block, or this whole stage is a no-op
try:
    import parsica_spirit.engine
    print("FAIL: engine import blocker is inert"); sys.exit(1)
except ImportError:
    pass

import parsica_spirit as pc
root = tempfile.mkdtemp()
ctx = pc.open_markdown(root)
r = ctx.store("deploy api_key=sk-ant-AAAAAAAAAAAAAAAAAAAAAAAA plus bruxism notes", labels={"c": "ops"})
assert r.redactions >= 1 and "sk-ant-" not in ctx.get(r.id).content, "scrub gate"
assert ctx.query("bruxism"), "retrieval"
ctx.continuity.save_working_set("## Pinned\n- clean-room base tier works\n")
assert "clean-room base tier works" in pc.open_markdown(root).continuity.restore(), "continuity"
ident = ctx.scope_identity("CleanRoomAgent")
assert ident.identity_id and ctx.identity.identity_id == ident.identity_id, "identity scoped-once"
# identity write path is scrub-gated too: a secret in a display name OR provenance must NOT reach disk
import os as _os
iroot = tempfile.mkdtemp()
pc.open_markdown(iroot).scope_identity("agent sk-ant-BBBBBBBBBBBBBBBBBBBBBBBB label",
                                       provenance="from token ghp_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
_raw = open(_os.path.join(iroot, "identity.json"), encoding="utf-8").read()
assert "sk-ant-" not in _raw and "ghp_" not in _raw, "a secret in a display name/provenance must be scrubbed"
# labels are an on-disk write path too: a secret in a label must NOT reach records.jsonl
lroot = tempfile.mkdtemp()
pc.open_markdown(lroot).store("a clean note", labels={"k": "api_key=sk-ant-CCCCCCCCCCCCCCCCCCCCCCCC"})
_lraw = open(_os.path.join(lroot, "records.jsonl"), encoding="utf-8").read()
assert "sk-ant-" not in _lraw, "a secret in a label must be scrubbed before records.jsonl"
# the deliberate blank-glyph split evasion is closed on the store path (braille-blank cell U+2800)
zroot = tempfile.mkdtemp()
zc = pc.open_markdown(zroot)
zr = zc.store("deploy sk-ant-\u2800DDDDDDDDDDDDDDDDDDDDDDDD note")
assert zr.redactions >= 1 and "DDDDDDDDDDDDDDDDDDDDDDDD" not in zc.get(zr.id).content, "blank-glyph scrub evasion"
# the whole lifecycle ran engine-dark: no engine module may have slipped into sys.modules
assert not any(m == "parsica_spirit.engine" or m.startswith("parsica_spirit.engine.")
               for m in sys.modules), "engine modules loaded during the base lifecycle"
print("CLEAN-ROOM OK")
'''

SMOKE_ENGINE = r'''
import tempfile
from parsica_spirit import Context, open_markdown
from parsica_spirit.engine.seam import EngineBackend
ctx = Context(EngineBackend(tempfile.mkdtemp()))
r = ctx.store("engine tier note with key sk-ant-EEEEEEEEEEEEEEEEEEEEEEEE inside")
assert r.redactions >= 1, "engine-tier scrub gate"
assert ctx.query("engine tier note"), "engine-tier retrieval"
assert ctx.tier == "EngineBackend", "tier reporting"
base = open_markdown(tempfile.mkdtemp())
assert base.tier == "MarkdownBackend", "base tier still the default surface"
print("ENGINE-TIER OK")
'''


def _run(cmd, **kw):
    print("+ " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, check=True, **kw)


def _venv_python(vdir):
    return os.path.join(vdir, "Scripts", "python.exe") if os.name == "nt" else os.path.join(vdir, "bin", "python")


def main():
    work = tempfile.mkdtemp(prefix="parsica_cleanroom_")
    wheelhouse = os.path.join(work, "wheelhouse")
    os.makedirs(wheelhouse)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}   # true clean room: no source-dir leak
    env["PYTHONUTF8"] = "1"

    # ---- STAGE 1: build the ONE wheel and verify artifact closure ---------------------
    _run([sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", wheelhouse, ROOT])
    wheels = glob.glob(os.path.join(wheelhouse, "*.whl"))
    if len(wheels) != 1:
        print("FAIL: expected exactly one wheel, built %d: %r" % (len(wheels), wheels))
        return 1
    whl = wheels[0]

    _selftest_closure()
    with zipfile.ZipFile(whl) as z:
        leaks = _closure_leaks(z.namelist())
    if leaks:
        print("FAIL (wheel-inventory closure):\n  " + "\n  ".join(leaks))
        return 1

    bad_deps = _metadata_deps(whl)
    if bad_deps:
        print("FAIL (zero-dependency promise): wheel METADATA declares runtime dependencies:\n  "
              + "\n  ".join(bad_deps))
        return 1

    # ---- STAGE 2: fresh venv, the real user command, base lifecycle engine-dark -------
    # --no-index makes the zero-dependency promise LIVE: if a runtime dependency ever sneaks
    # into the metadata, this exact install fails to resolve it and the gate goes red.
    vdir = os.path.join(work, "venv")
    venv.create(vdir, with_pip=True)
    vpy = _venv_python(vdir)
    _run([vpy, "-m", "pip", "install", "--no-index", "--find-links", wheelhouse, "parsica-spirit"])

    freeze = subprocess.run([vpy, "-m", "pip", "freeze"], capture_output=True, text=True, check=True).stdout
    extra = [ln for ln in freeze.splitlines() if ln.strip() and not ln.lower().startswith("parsica-spirit")]
    if "parsica-spirit" not in freeze.lower() or extra:
        print("FAIL: venv must hold parsica-spirit and nothing else (freeze: %s)" % freeze.strip())
        return 1

    run_dir = os.path.join(work, "run")
    os.makedirs(run_dir)
    smoke_py = os.path.join(run_dir, "smoke.py")
    with open(smoke_py, "w", encoding="utf-8") as f:
        f.write(SMOKE)
    out = subprocess.run([vpy, smoke_py], capture_output=True, text=True, env=env)
    print(out.stdout.strip())
    if out.returncode != 0 or "CLEAN-ROOM OK" not in out.stdout:
        print("FAIL (stage 2 base lifecycle, engine dark):\n" + (out.stderr or "")[-2000:])
        return 1

    # ---- STAGE 3: engine tier in the same install --------------------------------------
    smoke2_py = os.path.join(run_dir, "smoke2.py")
    with open(smoke2_py, "w", encoding="utf-8") as f:
        f.write(SMOKE_ENGINE)
    out2 = subprocess.run([vpy, smoke2_py], capture_output=True, text=True, env=env)
    print(out2.stdout.strip())
    if out2.returncode != 0 or "ENGINE-TIER OK" not in out2.stdout:
        print("FAIL (stage 3 engine smoke):\n" + (out2.stderr or "")[-2000:])
        return 1

    print("\nCLEAN-ROOM CHECK PASSED: stage 1 (one wheel, inventory closed, zero runtime "
          "dependencies declared) + stage 2 (base lifecycle runs with the engine import-blocked) "
          "+ stage 3 (the same install escalates to the engine tier).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
