"""Standing import-closure gate for the engine build.

Enforces an invariant: the build must never ship - or unguardedly hard-import - any module on a
configured exclusion set. A breach fails here in CI, not later in a downstream user's process with
a confusing ModuleNotFoundError.

The exclusion set is data-driven (see closure_excluded.local.txt) rather than hard-coded, so this
public test names no excluded module and a build re-sync is checked automatically.

What it enforces:
  1. No UNGUARDED import of an excluded module (a lazy import wrapped in try/except ImportError|
     Exception is allowed - it degrades gracefully; a module-level/unguarded one is a breach).
  2. Excluded module FILES are physically absent (a package ships its own .py, so absence here ==
     absence in the built wheel - the distribution-closure check for source modules).
  3. The public __init__ surface references no excluded module, not even guarded.
  4. No dynamic import (importlib.import_module / __import__) names an excluded module via a literal.
"""

import ast
import pathlib

import pytest

import parsica_spirit.engine

PKG = pathlib.Path(parsica_spirit.engine.__file__).parent

# Module names the build must never ship or hard-import. Kept as DATA in a gitignored, site-local
# sidecar (one name per line; blank lines and # comments ignored) so this public file enumerates
# none of them (the names are themselves a tell) and a build re-sync is checked automatically.
#
# The exclusion vocabulary ships with NObody: the sidecar is gitignored, so a public checkout has
# no names to run against. Rather than pass VACUOUSLY on an empty set (green while scanning nothing,
# which GPT's review rightly flagged), every test in this module SKIPS VISIBLY when the sidecar is
# absent - the gate is honestly reported "not armed" instead of silently satisfied. The armed run
# happens release-side, where the sidecar is present (and verify_release.py fails closed if it is
# not), so a real breach is still caught before anything ships.
def _load_excluded():
    sidecar = pathlib.Path(__file__).with_name("closure_excluded.local.txt")
    if not sidecar.exists():
        return set()
    return {ln.strip() for ln in sidecar.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.strip().startswith("#")}


EXCLUDED = _load_excluded()

# Skip VISIBLY (never pass vacuously) when the site-local exclusion sidecar is absent.
pytestmark = pytest.mark.skipif(
    not EXCLUDED,
    reason="exclusion sidecar absent - closure gate not armed (armed release-side, where the "
           "sidecar is present; verify_release.py fails closed without it)")

_CATCHES = {"ImportError", "ModuleNotFoundError", "Exception"}


def _excluded_ref(node):
    """The excluded module name an Import/ImportFrom node references, else None."""
    if isinstance(node, ast.ImportFrom):
        if node.module:
            for part in node.module.split("."):
                if part in EXCLUDED:
                    return part
        for alias in node.names:                       # from . import <excluded_module>
            if alias.name in EXCLUDED:
                return alias.name
    elif isinstance(node, ast.Import):
        for alias in node.names:
            for part in alias.name.split("."):
                if part in EXCLUDED:
                    return part
    return None


def _guarded_import_ids(tree):
    """id()s of import nodes lexically inside a try BODY whose except catches ImportError|Exception.

    Imports in else/finally/handler bodies are intentionally NOT counted (they run unconditionally
    or on failure, so they don't earn the guard exemption). Guard status is only a SECONDARY
    gate: the excluded-import test requires the ref be ALLOWLISTED first, so even an over-broad
    guard can never exempt an unlisted excluded import.
    """
    guarded = set()
    for t in ast.walk(tree):
        if not isinstance(t, ast.Try):
            continue
        catches = False
        for h in t.handlers:
            if h.type is None:                          # bare except
                catches = True
            elif isinstance(h.type, ast.Name) and h.type.id in _CATCHES:
                catches = True
            elif isinstance(h.type, ast.Tuple):
                catches = catches or any(
                    isinstance(e, ast.Name) and e.id in _CATCHES for e in h.type.elts)
        if not catches:
            continue
        for stmt in t.body:
            for n in ast.walk(stmt):
                if isinstance(n, (ast.Import, ast.ImportFrom)):
                    guarded.add(id(n))
    return guarded


def _all_py():
    return sorted(PKG.rglob("*.py"))


# Explicit allowlist of KNOWN, intentionally-guarded references to excluded modules. Each entry is
# a lazy import wrapped in try/except so an unavailable module degrades rather than crashing. Any
# excluded reference NOT on this list - guarded or not - is a closure breach; an allowlisted
# reference that loses its guard is also a breach. Kept as DATA so a build re-sync that introduces
# a NEW excluded reference fails here until it is scrubbed or deliberately allowlisted (and
# reviewed).
# Keys are (package-relative posix path, excluded module name) - path-qualified, not bare basename,
# so a same-named file elsewhere in the package cannot inherit an exemption.
ALLOWED_GUARDED = set()


def test_excluded_imports_are_allowlisted_and_guarded():
    """No reference to an excluded module unless it is BOTH explicitly allowlisted AND guarded.

    A blanket 'guarded imports are exempt' rule is a false negative: a wheel that dropped an
    excluded module would still import green and run silently degraded. So an excluded reference
    must be enumerated here (intentional, deferred) AND genuinely guarded; anything else fails.
    """
    violations = []
    for py in _all_py():
        tree = ast.parse(py.read_text(encoding="utf-8"))
        guarded = _guarded_import_ids(tree)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            name = _excluded_ref(node)
            if not name:
                continue
            key = (py.relative_to(PKG).as_posix(), name)
            if key not in ALLOWED_GUARDED:
                violations.append("%s:%d excluded %r is not on the allowlist" % (py.name, node.lineno, name))
            elif id(node) not in guarded:
                violations.append("%s:%d allowlisted %r lost its try/except guard" % (py.name, node.lineno, name))
    assert not violations, "closure breach:\n" + "\n".join(violations)


def test_excluded_modules_are_physically_absent():
    present = sorted(p.name for p in _all_py() if p.stem in EXCLUDED)
    assert not present, "excluded module files shipped in the build: %s" % present


def test_public_surface_references_no_excluded_module():
    tree = ast.parse((PKG / "__init__.py").read_text(encoding="utf-8"))
    bad = ["line %d: %s" % (n.lineno, _excluded_ref(n)) for n in ast.walk(tree)
           if isinstance(n, (ast.Import, ast.ImportFrom)) and _excluded_ref(n)]
    assert not bad, "the public __init__ surface must not reference excluded modules: %s" % bad


def test_no_unvettable_or_excluded_dynamic_import():
    """Dynamic imports (importlib.import_module / __import__) must use a STATIC literal, and that
    literal must not name an excluded module. A non-literal argument (concat / f-string) cannot be
    vetted for closure, so it fails too - forcing dynamic dispatch to stay statically checkable.
    Resolves `from importlib import import_module [as x]` aliases as well as the attribute form."""
    bad = []
    for py in _all_py():
        tree = ast.parse(py.read_text(encoding="utf-8"))
        dyn_names = {"__import__"}                        # names bound to a dynamic importer here
        for n in ast.walk(tree):
            if isinstance(n, ast.ImportFrom) and n.module == "importlib":
                for a in n.names:
                    if a.name == "import_module":
                        dyn_names.add(a.asname or a.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            is_dyn = (isinstance(fn, ast.Attribute) and fn.attr == "import_module") or \
                     (isinstance(fn, ast.Name) and fn.id in dyn_names)
            if not is_dyn or not node.args:
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                for part in arg.value.split("."):
                    if part in EXCLUDED:
                        bad.append("%s:%d dynamic import names excluded %r" % (py.name, node.lineno, part))
            else:
                bad.append("%s:%d dynamic import with a non-literal argument (cannot vet for closure)"
                           % (py.name, node.lineno))
    assert not bad, "closure breach (dynamic import):\n" + "\n".join(bad)
