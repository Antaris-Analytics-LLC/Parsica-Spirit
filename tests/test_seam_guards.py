"""Mechanical guards for the package's internal zones - discovery-based, so a new module
is covered the moment it exists.

One package, three zones: the contracts leaf (parsica_spirit.enrich), the engine
(parsica_spirit.engine), and the facade (every other module - the base tier's surface,
the package __init__ included). The seam's whole purpose is RUNTIME binding via
dependency injection: the application builds the engine backend and hands it to the
facade as a StoragePort. So the facade must never import OR dynamically load the engine,
the leaf must import nothing else in the package, and the engine may reach only the leaf.

The `.importlinter` contracts assert the same directions in CI; because import-linter
needs enumerated module names, a sync test here DISCOVERS the facade set from the source
tree and fails the moment the enumeration goes stale - the config can never quietly stop
covering a new module. These greps are the fast heuristic layer (they catch the common
vectors, not exotic ones like exec-assembled module names); the discovery walk plus the
literal-name check is what makes them hard to dodge.
"""

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "src", "parsica_spirit"))
ENGINE_DOTTED = "parsica_spirit" + ".engine"      # assembled so THIS file passes its own literal ban

_ENGINE_IMPORT = re.compile(r"^\s*(from|import)\s+parsica_spirit\.engine\b", re.M)
_DYNLOAD = re.compile(r"import_module\s*\(|entry_points\s*\(|__import__\s*\(|\bimportlib\b", re.M)
# any import of parsica_spirit EXCEPT the leaf itself (absolute self-imports are allowed)
_NON_LEAF_IMPORT = re.compile(r"^\s*(from|import)\s+parsica_spirit(?!\.enrich\b)", re.M)
# any import of parsica_spirit EXCEPT the leaf and the engine itself
_NON_ENGINE_IMPORT = re.compile(r"^\s*(from|import)\s+parsica_spirit(?!\.(enrich|engine)\b)", re.M)


def _py_files(root, skip_dirs=()):
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip_dirs and d != "__pycache__"]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(dirpath, f)


def _facade_modules():
    """DISCOVER the facade module set: every top-level module or subpackage of
    parsica_spirit except the engine and the leaf. New code is covered on creation."""
    found = set()
    for entry in sorted(os.listdir(SRC)):
        if entry in ("engine", "enrich", "__pycache__", "__init__.py"):
            continue
        path = os.path.join(SRC, entry)
        if entry.endswith(".py"):
            found.add("parsica_spirit." + entry[:-3])
        elif os.path.isdir(path) and os.path.exists(os.path.join(path, "__init__.py")):
            found.add("parsica_spirit." + entry)
    return found


def test_facade_never_imports_or_dynamically_loads_the_engine():
    """The facade (package __init__ included) must not import, dynamically load, or even
    literally name the engine subpackage - the engine backend arrives by injection only."""
    for p in _py_files(SRC, skip_dirs=("engine", "enrich")):
        text = open(p, encoding="utf-8").read()
        assert not _ENGINE_IMPORT.search(text), p + ": facade imports the engine (must be injected)"
        assert not _DYNLOAD.search(text), p + ": facade dynamically loads modules (engine must be injected)"
        assert ENGINE_DOTTED not in text, p + ": facade names the engine subpackage literally"


def test_leaf_imports_nothing_else_in_the_package():
    """The contracts leaf stays a pure leaf: standard library plus itself, nothing more."""
    for p in _py_files(os.path.join(SRC, "enrich")):
        text = open(p, encoding="utf-8").read()
        assert not _NON_LEAF_IMPORT.search(text), p + " (leaf) imports beyond the leaf"
        assert ENGINE_DOTTED not in text, p + " (leaf) names the engine subpackage literally"


def test_engine_imports_only_the_leaf_and_itself():
    """The engine reaches the rest of the package only through the leaf contracts - it
    must never import the facade (that direction is the seam inverted)."""
    for p in _py_files(os.path.join(SRC, "engine")):
        text = open(p, encoding="utf-8").read()
        assert not _NON_ENGINE_IMPORT.search(text), p + " (engine) imports the facade"


def test_importlinter_enumeration_matches_discovered_facade():
    """.importlinter needs enumerated module names; this test is what keeps that honest.
    Every DISCOVERED facade module must appear in every contract section of the config,
    so a new module cannot dodge the import contracts by being new."""
    cfg_path = os.path.normpath(os.path.join(HERE, "..", ".importlinter"))
    cfg = open(cfg_path, encoding="utf-8").read()
    discovered = _facade_modules()
    assert discovered, "facade discovery found nothing - the walk itself is broken"
    sections = cfg.split("[importlinter:contract:")[1:]
    assert len(sections) >= 2, "expected at least two contracts in .importlinter"
    for section in sections:
        title = section.splitlines()[0]
        listed = {ln.strip() for ln in section.splitlines() if ln.strip().startswith("parsica_spirit")}
        missing = sorted(discovered - listed)
        assert not missing, (
            ".importlinter contract %r is missing discovered facade module(s) %s - "
            "add them so the direction contracts keep covering the whole facade" % (title, missing))
        assert ENGINE_DOTTED in listed, (
            ".importlinter contract %r does not mention the engine subpackage" % title)


def test_reexport_is_the_canonical_leaf_contract():
    """The facade's convenience re-export must BE the leaf's contract object, not a copy."""
    import parsica_spirit
    import parsica_spirit.ports as cports
    import parsica_spirit.enrich

    for name in ("StoragePort", "AdminBackend", "StoredRecord", "ResetPlan", "PlanError"):
        assert getattr(cports, name) is getattr(parsica_spirit.enrich, name), name + " diverged in ports re-export"
        assert getattr(parsica_spirit, name) is getattr(parsica_spirit.enrich, name), name + " diverged in package re-export"
