"""parsica_spirit.claude_memory - import Claude Code's own memory into Parsica.

Claude Code keeps memory as plain markdown: instruction files (``CLAUDE.md`` /
``CLAUDE.local.md``, and a user-level ``~/.claude/CLAUDE.md``) and auto-memory (a per-project
``MEMORY.md`` index plus optional topic files). This module reads those files, splits them into
provenance-stamped records, and imports them into a Parsica ``Context`` (base tier or engine tier
alike - through the public facade, so the secret/PII scrub applies to every write). A host with
its own fence beyond that scrub - a refusal list, a do-not-index rule - enforces it by passing a
``transform`` to ``import_records`` (below); Spirit stays policy-agnostic and never assumes a
caller's fence.

Design (deliberately conservative for the mix-CC-memory-with-Parsica use case):
  * IMPORT-ONLY. Claude Memory -> Parsica, never the reverse. Those files are user-owned; nothing
    here writes back to CLAUDE.md / MEMORY.md / topic files.
  * PROVENANCE on every record: source, source_path, source_scope, source_kind, heading_path,
    content_hash. Kept in labels; the idempotency keys are hex (scrub-stable), so re-import matches
    even though a path label may be redacted.
  * IDEMPOTENT. Keyed on (source_path + heading_path). Re-import SKIPS an unchanged section, UPDATES
    a changed one, and (opt-in) FORGETS a section that disappeared from the files - never duplicates.
  * The instruction-vs-fact distinction is preserved (record_class label): a CLAUDE.md rule is an
    instruction, not a high-confidence fact, unless a human promotes it. This module never promotes.
"""
from __future__ import annotations

import hashlib
import os
import re
from collections import Counter
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_FENCE_RE = re.compile(r"^\s*(```+|~~~+)")
_ATX_CLOSE_RE = re.compile(r"\s+#+\s*$")   # a SPACE-delimited trailing #### is decorative

SOURCE = "claude-code-memory"

# scope -> (source_kind, record_class) for the known Claude memory shapes
_INSTRUCTION = "instruction"
_MEMORY = "memory"


@dataclass(frozen=True)
class ClaudeRecord:
    """One imported section of a Claude memory file, with its provenance."""
    source_path: str
    source_scope: str          # project | local | user | auto
    source_kind: str           # claude_md_instruction | claude_local_instruction |
    #                            claude_auto_memory_index | claude_auto_memory_topic
    record_class: str          # instruction | memory
    heading_path: Tuple[str, ...]
    body: str
    source_mtime: float
    occurrence: int = 0        # nth section with this heading_path in this file (0-based)

    @property
    def content(self) -> str:
        """Stored text. It leads with a PII-free provenance tag (scope, heading trail, occurrence,
        and a HASH of the source path) so that two sections with identical bodies but DIFFERENT
        provenance render to different text -- the base tier keys its record id on the content hash,
        so without this a duplicate heading, shared boilerplate across projects, or the same section
        in project + user CLAUDE.md would collide onto one record and silently overwrite provenance."""
        head = " / ".join(self.heading_path)
        src8 = hashlib.sha256(self.source_path.encode("utf-8")).hexdigest()[:8]
        occ = ("#%d" % self.occurrence) if self.occurrence else ""
        label = (head + occ) if head else ("(preamble)" + occ)
        return "[claude %s \u00b7 %s \u00b7 src:%s]\n\n%s" % (self.source_scope, label, src8, self.body)

    @property
    def provenance_key(self) -> str:
        """Stable identity of this section across imports: hash of source_path + heading_path +
        occurrence. Hex, so it survives label scrubbing and keys idempotency reliably. Occurrence
        makes two same-named headings in one file distinct slots."""
        raw = "%s\x00%s\x00#%d" % (self.source_path, "\x00".join(self.heading_path), self.occurrence)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()[:16]

    def labels(self, import_root: str) -> Dict[str, str]:
        return {
            "source": SOURCE,
            "import_root": import_root,          # scopes deletion reconciliation to this import
            "pk": self.provenance_key,           # idempotency slot (hex, scrub-stable)
            "content_hash": self.content_hash,   # change detection (hex, scrub-stable)
            "source_kind": self.source_kind,
            "source_scope": self.source_scope,
            "record_class": self.record_class,
            "heading_path": " / ".join(self.heading_path),
            "source_path": self.source_path,     # human-readable; may be scrubbed, never keyed on
        }


@dataclass
class ImportReport:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    deleted: int = 0
    redactions: int = 0

    def as_dict(self) -> dict:
        return {"added": self.added, "updated": self.updated, "unchanged": self.unchanged,
                "deleted": self.deleted, "redactions": self.redactions}


def split_markdown(text: str) -> List[Tuple[Tuple[str, ...], str]]:
    """Split markdown into (heading_path, body) sections. A section's heading_path is the trail of
    ancestor headings; its body is the text under that heading up to the next heading (of any level).
    Text before the first heading is a section with an empty heading_path. Heading-only sections
    (no body) are dropped."""
    sections: List[Tuple[Tuple[str, ...], str]] = []
    stack: List[Tuple[int, str]] = []
    cur_path: Tuple[str, ...] = ()
    cur_body: List[str] = []
    fence: Optional[str] = None            # the fence marker (``` or ~~~) currently open, else None
    for line in text.splitlines():
        fm = _FENCE_RE.match(line)
        if fm:
            marker = fm.group(1)[:3]
            if fence is None:
                fence = marker
            elif line.lstrip().startswith(fence):
                fence = None
            cur_body.append(line)
            continue
        m = None if fence is not None else _HEADING_RE.match(line)   # never a heading inside a fence
        if m:
            body = "\n".join(cur_body).strip()
            if body:
                sections.append((cur_path, body))
            level = len(m.group(1))
            title = _ATX_CLOSE_RE.sub("", m.group(2).strip()).strip() or m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            cur_path = tuple(t for _, t in stack)
            cur_body = []
        else:
            cur_body.append(line)
    body = "\n".join(cur_body).strip()
    if body:
        sections.append((cur_path, body))
    return sections


def parse_file(path: str, *, scope: str, kind: str, record_class: str) -> List[ClaudeRecord]:
    """Parse one markdown file into records. Missing/unreadable file -> []."""
    try:
        text = open(path, encoding="utf-8", errors="replace").read()
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    rp = os.path.abspath(path)
    seen: Counter = Counter()
    out: List[ClaudeRecord] = []
    for hp, body in split_markdown(text):
        occ = seen[hp]
        seen[hp] += 1
        out.append(ClaudeRecord(source_path=rp, source_scope=scope, source_kind=kind,
                                record_class=record_class, heading_path=hp, body=body,
                                source_mtime=mtime, occurrence=occ))
    return out


def discover_records(*, project_root: Optional[str] = None, user_dir: Optional[str] = None,
                     memory_dir: Optional[str] = None) -> List[ClaudeRecord]:
    """Find and parse the known Claude memory files.

      project_root: dir holding CLAUDE.md (project) and/or CLAUDE.local.md (local).
      user_dir:     dir holding a user-level CLAUDE.md (e.g. ~/.claude).
      memory_dir:   dir holding MEMORY.md (auto index) + topic *.md (auto topics).
    """
    out: List[ClaudeRecord] = []
    if project_root:
        out += parse_file(os.path.join(project_root, "CLAUDE.md"),
                          scope="project", kind="claude_md_instruction", record_class=_INSTRUCTION)
        out += parse_file(os.path.join(project_root, "CLAUDE.local.md"),
                          scope="local", kind="claude_local_instruction", record_class=_INSTRUCTION)
    if user_dir:
        out += parse_file(os.path.join(user_dir, "CLAUDE.md"),
                          scope="user", kind="claude_md_instruction", record_class=_INSTRUCTION)
    if memory_dir and os.path.isdir(memory_dir):
        index = os.path.join(memory_dir, "MEMORY.md")
        out += parse_file(index, scope="auto", kind="claude_auto_memory_index", record_class=_MEMORY)
        for fn in sorted(os.listdir(memory_dir)):
            full = os.path.join(memory_dir, fn)
            if fn.endswith(".md") and fn != "MEMORY.md" and os.path.isfile(full):
                out += parse_file(full, scope="auto", kind="claude_auto_memory_topic",
                                  record_class=_MEMORY)
    return out


def import_records(ctx, records: List[ClaudeRecord], *, import_root: str = "claude",
                   delete_missing: bool = False, transform=None) -> ImportReport:
    """Idempotently import records into a Context. Keyed on provenance_key: an unchanged section is
    skipped, a changed one is replaced (forget old id + store new), a new one is stored. Never
    duplicates.

    ctx.store() applies the secret/PII scrub. Any policy BEYOND that scrub - a caller's refusal
    fence, a do-not-index rule - is the caller's to enforce via `transform`, because Spirit cannot
    know a host's fence (parsica-weave's workspace refusal union, its [[human-only]] interlock).
    `transform(record)` returns a possibly-rewritten record to store, or None to EXCLUDE it: an
    excluded section's prior copy is retired (targeted, by identity), so a rule added AFTER an
    earlier raw import cleans up the stale record instead of leaving it live.

    Identity (the idempotency slot) is taken from the ORIGINAL discovered record, so a transform
    that fences a body or heading updates the same slot in place rather than orphaning it; change
    detection keys on the STORED (transformed) content, so applying a new fence triggers an update.
    With delete_missing, a previously-imported section (same import_root) absent from `records` is
    also forgotten."""
    report = ImportReport()

    # index existing imported records for THIS import_root by provenance_key
    existing: Dict[str, Tuple[str, str]] = {}   # pk -> (record_id, content_hash)
    for r in ctx.all_records():
        lb = r.labels or {}
        if lb.get("source") == SOURCE and lb.get("import_root") == import_root and lb.get("pk"):
            existing[lb["pk"]] = (r.id, lb.get("content_hash", ""))

    seen: set = set()
    excluded: set = set()                       # discovered but transform-excluded: retire prior copy
    for rec in records:
        pk = rec.provenance_key                 # identity from the ORIGINAL record - stable under fencing
        stored = transform(rec) if transform is not None else rec
        if stored is None:
            excluded.add(pk)                    # e.g. now do-not-index: retire, never re-store
            continue
        seen.add(pk)
        labels = stored.labels(import_root)
        labels["pk"] = pk                       # keep the ORIGINAL identity even if the payload was fenced
        chash = stored.content_hash             # change detection keys on the STORED (fenced) content
        if pk in existing:
            old_id, old_hash = existing[pk]
            if old_hash == chash:
                report.unchanged += 1
                continue
            ctx.forget(old_id)                 # content or applied policy changed: replace in place
            res = ctx.store(stored.content, labels=labels)
            report.updated += 1
            report.redactions += getattr(res, "redactions", 0)
        else:
            res = ctx.store(stored.content, labels=labels)
            report.added += 1
            report.redactions += getattr(res, "redactions", 0)

    # targeted retirement of sections the transform excluded this pass (a do-not-index rule added
    # after an earlier import): forget ONLY those specific prior copies - never a blanket sweep, so
    # a transient unreadable file (parse_file -> []) can never mass-forget the pool.
    for pk in excluded:
        if pk in existing:
            ctx.forget(existing[pk][0])
            report.deleted += 1

    if delete_missing:
        for pk, (rid, _h) in existing.items():
            if pk not in seen and pk not in excluded:
                ctx.forget(rid)
                report.deleted += 1
    return report


def import_claude_memory(ctx, *, project_root: Optional[str] = None, user_dir: Optional[str] = None,
                         memory_dir: Optional[str] = None, import_root: Optional[str] = None,
                         delete_missing: bool = False, transform=None) -> ImportReport:
    """Convenience: discover the known Claude memory files and import them. `import_root` scopes
    idempotency + deletion to this source set (default: a stable hash of the discovery paths).
    `transform` (see import_records) lets a host enforce its own fence before storage."""
    if import_root is None:
        seed = "|".join(str(p) for p in (project_root, user_dir, memory_dir))
        import_root = "claude-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:10]
    records = discover_records(project_root=project_root, user_dir=user_dir, memory_dir=memory_dir)
    return import_records(ctx, records, import_root=import_root, delete_missing=delete_missing,
                          transform=transform)
