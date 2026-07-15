"""Claude Code memory import: parse real-shaped CLAUDE.md / MEMORY.md / topic files, import into a
Context, and prove the import is provenance-preserving, idempotent, scrubbed, refusal-fenced, and
read-only (it never writes back to the source files)."""
import os
import tempfile

from parsica_spirit import open_markdown, import_claude_memory, discover_records
from parsica_spirit.claude_memory import split_markdown, SOURCE


CLAUDE_MD = """# Project instructions

Use tabs, not spaces.

## Testing
Run pytest before every commit.

## Style
Prefer small functions.
"""

MEMORY_MD = """# Memory index

- [Auth flow](auth.md) - how login works
"""

TOPIC_MD = """# Auth flow

The login uses a JWT with a 60 second retry cap.
Contact sam@acme-example.io for the signing key.
"""


def _project():
    d = tempfile.mkdtemp(prefix="cm_proj_")
    open(os.path.join(d, "CLAUDE.md"), "w", encoding="utf-8").write(CLAUDE_MD)
    mem = os.path.join(d, "memory")
    os.makedirs(mem)
    open(os.path.join(mem, "MEMORY.md"), "w", encoding="utf-8").write(MEMORY_MD)
    open(os.path.join(mem, "auth.md"), "w", encoding="utf-8").write(TOPIC_MD)
    return d, mem


def test_split_markdown_by_heading_path():
    secs = dict(split_markdown(CLAUDE_MD))
    assert ("Project instructions",) in secs
    assert secs[("Project instructions", "Testing")] == "Run pytest before every commit."
    assert secs[("Project instructions", "Style")] == "Prefer small functions."


def test_discover_classifies_by_source_kind():
    d, mem = _project()
    recs = discover_records(project_root=d, memory_dir=mem)
    kinds = {r.source_kind for r in recs}
    assert "claude_md_instruction" in kinds
    assert "claude_auto_memory_index" in kinds
    assert "claude_auto_memory_topic" in kinds
    classes = {r.record_class for r in recs}
    assert classes == {"instruction", "memory"}


def test_import_is_provenance_preserving():
    d, mem = _project()
    ctx = open_markdown(tempfile.mkdtemp(prefix="cm_store_"))
    rep = import_claude_memory(ctx, project_root=d, memory_dir=mem)
    assert rep.added >= 4 and rep.updated == 0
    recs = [r for r in ctx.all_records() if (r.labels or {}).get("source") == SOURCE]
    assert recs, "records must carry the claude-code-memory source label"
    for r in recs:
        lb = r.labels
        assert lb["pk"] and lb["content_hash"] and lb["source_kind"] and lb["record_class"]


def test_reimport_is_idempotent():
    d, mem = _project()
    store = tempfile.mkdtemp(prefix="cm_idem_")
    ctx = open_markdown(store)
    rep1 = import_claude_memory(ctx, project_root=d, memory_dir=mem)
    n = len([r for r in ctx.all_records() if (r.labels or {}).get("source") == SOURCE])
    # a second import with NO changes adds nothing and duplicates nothing
    ctx2 = open_markdown(store)
    rep2 = import_claude_memory(ctx2, project_root=d, memory_dir=mem)
    assert rep2.added == 0 and rep2.updated == 0 and rep2.unchanged == rep1.added
    n2 = len([r for r in ctx2.all_records() if (r.labels or {}).get("source") == SOURCE])
    assert n2 == n, "re-import must not duplicate records"


def test_change_updates_in_place():
    d, mem = _project()
    store = tempfile.mkdtemp(prefix="cm_upd_")
    import_claude_memory(open_markdown(store), project_root=d, memory_dir=mem)
    # edit one section body
    open(os.path.join(d, "CLAUDE.md"), "w", encoding="utf-8").write(
        CLAUDE_MD.replace("Prefer small functions.", "Prefer small, pure functions."))
    ctx = open_markdown(store)
    rep = import_claude_memory(ctx, project_root=d, memory_dir=mem)
    assert rep.updated == 1 and rep.added == 0
    bodies = " ".join(r.content for r in ctx.all_records())
    assert "Prefer small, pure functions." in bodies
    assert bodies.count("Prefer small") == 1, "update must replace, not duplicate"


def test_delete_missing_forgets_removed_sections():
    d, mem = _project()
    store = tempfile.mkdtemp(prefix="cm_del_")
    import_claude_memory(open_markdown(store), project_root=d, memory_dir=mem)
    # remove the Style section
    open(os.path.join(d, "CLAUDE.md"), "w", encoding="utf-8").write(
        CLAUDE_MD.replace("\n## Style\nPrefer small functions.\n", "\n"))
    ctx = open_markdown(store)
    rep = import_claude_memory(ctx, project_root=d, memory_dir=mem, delete_missing=True)
    assert rep.deleted == 1
    assert "Prefer small functions." not in " ".join(r.content for r in ctx.all_records())


def test_import_scrubs_pii_and_never_writes_back():
    d, mem = _project()
    src = os.path.join(mem, "auth.md")
    before = open(src, encoding="utf-8").read()
    ctx = open_markdown(tempfile.mkdtemp(prefix="cm_scrub_"))
    import_claude_memory(ctx, project_root=d, memory_dir=mem)
    blob = " ".join(r.content for r in ctx.all_records())
    assert "sam@acme-example.io" not in blob, "PII must be scrubbed before storage"
    assert "[EMAIL]" in blob or "REDACTED" in blob.upper()
    # read-only: the source file is untouched
    assert open(src, encoding="utf-8").read() == before


def test_import_applies_refusal_list():
    d, mem = _project()
    store = tempfile.mkdtemp(prefix="cm_ref_")
    # a refusal term in the store config would be applied by the engine; here we assert the content
    # path goes through scrub by checking a planted secret in an instruction is redacted
    open(os.path.join(d, "CLAUDE.md"), "a", encoding="utf-8").write(
        "\n## Secrets\napi key sk-ant-AAAAAAAAAAAAAAAAAAAAAAAA must not leak\n")
    ctx = open_markdown(store)
    import_claude_memory(ctx, project_root=d, memory_dir=mem)
    blob = " ".join(r.content for r in ctx.all_records())
    assert "sk-ant-AAAAAAAAAAAAAAAAAAAAAAAA" not in blob


# --- regression tests ---

def test_identical_sections_across_sources_stay_distinct():
    """CRITICAL regression: the same section text in two different sources must be TWO records with
    two provenances, not one collapsing over the other (base tier keys id on content)."""
    store = tempfile.mkdtemp(prefix="cm_coll_")
    a = tempfile.mkdtemp(prefix="cm_a_"); b = tempfile.mkdtemp(prefix="cm_b_")
    same = "# Testing\n\nRun pytest before every commit.\n"
    open(os.path.join(a, "CLAUDE.md"), "w", encoding="utf-8").write(same)
    open(os.path.join(b, "CLAUDE.md"), "w", encoding="utf-8").write(same)
    import_claude_memory(open_markdown(store), project_root=a, import_root="A")
    import_claude_memory(open_markdown(store), project_root=b, import_root="B")
    ctx = open_markdown(store)
    recs = [r for r in ctx.all_records() if (r.labels or {}).get("source") == SOURCE]
    roots = {r.labels["import_root"] for r in recs}
    assert roots == {"A", "B"}, "both sources must survive; got %r" % roots


def test_duplicate_headings_in_one_file_stay_distinct():
    d = tempfile.mkdtemp(prefix="cm_dup_")
    open(os.path.join(d, "CLAUDE.md"), "w", encoding="utf-8").write(
        "# Doc\n\n## Notes\nFirst notes.\n\n## Notes\nSecond notes.\n")
    store = tempfile.mkdtemp(prefix="cm_dupstore_")
    rep = import_claude_memory(open_markdown(store), project_root=d)
    # both 'Notes' sections present, and a NO-CHANGE re-import must be fully idempotent
    ctx = open_markdown(store)
    rep2 = import_claude_memory(ctx, project_root=d)
    assert rep2.added == 0 and rep2.updated == 0, "duplicate headings must not oscillate: %r" % rep2.as_dict()
    blob = " ".join(r.content for r in ctx.all_records())
    assert "First notes." in blob and "Second notes." in blob


def test_code_fence_hashes_are_not_headings():
    secs = split_markdown("# Real\n\nText.\n\n```bash\n# not a heading\necho hi\n```\n\nAfter.\n")
    paths = [hp for hp, _ in secs]
    assert ("Real",) in paths
    assert ("not a heading",) not in paths, "a # inside a code fence must not become a heading"
    body = dict(secs)[("Real",)]
    assert "# not a heading" in body and "echo hi" in body and "After." in body


def test_trailing_atx_close_stripped_but_sharp_kept():
    secs = dict(split_markdown("## Title ##\nbody\n"))
    assert ("Title",) in secs and ("Title ##",) not in secs
    secs2 = [hp for hp, _ in split_markdown("## C# language\ncode\n")]
    assert ("C# language",) in secs2, "an internal # (C#) must be preserved"
