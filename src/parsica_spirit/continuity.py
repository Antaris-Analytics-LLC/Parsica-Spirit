"""parsica_spirit.continuity - the markdown continuity surface (engine ABSENT).

Working set (a living doc, replaced in full), a permanent journal, and lessons - all plain
markdown files, SCRUBBED on write and SCRUBBED-then-sliced on restore. Continuity therefore
survives compaction and a fresh session with no engine involved and without ever hoarding a
secret. This is the library form of the hook lifecycle: PreCompact preserves (the files are
already on disk; a boundary marker is dropped), SessionStart re-injects the budgeted blob.

Two invariants carried over from the live skill:
  * scrub-then-slice - the FULL text is scrubbed before it is tailed, so a secret straddling
    the byte cut can never leave a prefix-stripped high-entropy remainder in the re-inject.
  * real-content filter - an unfilled template (only headers / comments / italic notes) never
    produces a re-inject, so a fresh workspace stays quiet instead of echoing boilerplate.
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timezone
from typing import List, Optional

from .scrub import scrub

_DEFAULT_BUDGET = 16000
_LESSONS_BYTES = 4000
_JOURNAL_BYTES = 3000
_ISO_DATE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")   # \Z, not $, so a trailing newline can't sneak in
_TRUNC_MARK = "...(earlier content truncated)\n"
# "--!>" closes a comment too (HTML5 "incorrectly closed comment"), so it is a breakout as much as "-->".
_COMMENT_BREAKOUT = re.compile(r"--!?>|<!--|[\r\n]")


def _safe_comment(s: str) -> str:
    """Neutralize HTML-comment breakout / injection for a value embedded inside <!-- ... -->."""
    return _COMMENT_BREAKOUT.sub(" ", s or "")


def _require_iso_date(date: str) -> None:
    """Journal day-files sort chronologically only if their names are ISO YYYY-MM-DD."""
    if not _ISO_DATE.match(date or ""):
        raise ValueError("journal date must be ISO YYYY-MM-DD (got %r)" % (date,))


def _has_real_content(md_text: str) -> bool:
    """True once a markdown file holds a line that isn't a header, comment, or italic note."""
    for line in md_text.splitlines():
        s = line.strip()
        if (not s or s.startswith("#") or s.startswith("<!--")
                or s.endswith("-->") or (s.startswith("_") and s.endswith("_"))):
            continue
        return True
    return False


class Continuity:
    """The markdown-tier continuity surface, rooted at a per-workspace directory."""

    def __init__(self, root: str, *, scrub_pii: bool = True) -> None:
        self._root = root
        self._scrub_pii = scrub_pii
        os.makedirs(root, exist_ok=True)
        self._working = os.path.join(root, "working.md")
        self._lessons = os.path.join(root, "lessons.md")
        self._journal_dir = os.path.join(root, "journal")

    # --- write path (scrub on write) ---
    def save_working_set(self, text: str) -> int:
        """Replace the working set in full, ATOMICALLY. Returns the scrub redaction count."""
        clean, n = scrub(text or "", scrub_pii=self._scrub_pii)
        d = os.path.dirname(self._working) or "."
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".working-", suffix=".tmp")   # unique per call
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(clean)
            os.replace(tmp, self._working)   # atomic swap - a crash mid-write can't corrupt the live doc
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return n

    def append_lesson(self, text: str) -> int:
        clean, n = scrub(text or "", scrub_pii=self._scrub_pii)
        with open(self._lessons, "a", encoding="utf-8") as f:
            f.write("\n" + clean.rstrip() + "\n")
        return n

    def append_journal(self, text: str, *, date: Optional[str] = None) -> int:
        date = date or datetime.now(timezone.utc).date().isoformat()
        _require_iso_date(date)
        clean, n = scrub(text or "", scrub_pii=self._scrub_pii)
        os.makedirs(self._journal_dir, exist_ok=True)
        with open(os.path.join(self._journal_dir, f"{date}.md"), "a", encoding="utf-8") as f:
            f.write("\n" + clean.rstrip() + "\n")
        return n

    def mark_compaction_boundary(self, *, date: Optional[str] = None, stamp: Optional[str] = None) -> None:
        """PreCompact: the working set + journal already persist on disk; drop a boundary marker.
        The marker line is scrubbed too - nothing reaches disk on the write path unscrubbed."""
        now = datetime.now(timezone.utc)
        date = date or now.date().isoformat()
        stamp = stamp or now.strftime("%Y-%m-%d %H:%M UTC")
        _require_iso_date(date)
        stamp = _safe_comment(stamp)   # a caller-supplied stamp can't break out of the <!-- --> comment
        marker, _ = scrub(
            "<!-- %s - compaction boundary; working set & journal preserved -->" % stamp,
            scrub_pii=self._scrub_pii,
        )
        os.makedirs(self._journal_dir, exist_ok=True)
        with open(os.path.join(self._journal_dir, f"{date}.md"), "a", encoding="utf-8") as f:
            f.write("\n" + marker + "\n")

    # --- restore path (scrub-then-slice, budgeted) ---
    def _scrub_tail(self, path: str, max_bytes: int):
        """Return (scrubbed_tail, truncated). max_bytes<=0 -> ("", False) - never let b[-0:] alias
        to the whole file. The FULL text is scrubbed BEFORE slicing (byte-boundary safe)."""
        if max_bytes <= 0:
            return "", False
        try:
            with open(path, encoding="utf-8") as f:
                b = f.read()
        except OSError:
            return "", False
        b, _ = scrub(b, scrub_pii=self._scrub_pii)
        if len(b) <= max_bytes:
            return b, False
        return b[-max_bytes:], True

    def _section(self, path: str, max_bytes: int) -> Optional[str]:
        """A restore section body, or None when the surviving tail holds no real content. The
        truncation marker is added AFTER the real-content test - so a boilerplate-only tail stays
        quiet, and the marker itself never counts as content."""
        text, truncated = self._scrub_tail(path, max_bytes)
        if not _has_real_content(text):
            return None
        return (_TRUNC_MARK + text if truncated else text).strip()

    def restore(self, *, budget: int = _DEFAULT_BUDGET) -> str:
        """Assemble the SessionStart re-inject: working set (<= half the budget) + lessons + latest
        journal, each scrubbed-then-sliced, the whole blob defense-scrubbed and capped WITHIN budget.
        Returns "" when nothing real has been written yet, or when budget <= 0."""
        if budget <= 0:
            return ""
        sections: List[str] = []
        working = self._section(self._working, budget // 2)
        if working:
            sections.append("### Working set (pinned facts + open loops)\n" + working)
        lessons = self._section(self._lessons, _LESSONS_BYTES)
        if lessons:
            sections.append("### Lessons / scars\n" + lessons)
        if os.path.isdir(self._journal_dir):
            jfiles = sorted((f for f in os.listdir(self._journal_dir) if f.endswith(".md")), reverse=True)
            if jfiles:
                jtext = self._section(os.path.join(self._journal_dir, jfiles[0]), _JOURNAL_BYTES)
                if jtext:   # skip a day-file holding only a boundary marker
                    sections.append("### Latest journal (%s)\n%s" % (jfiles[0][:-3], jtext))
        if not sections:
            return ""
        blob = "[parsica-spirit] Continuity restored (survives compaction).\n\n" + "\n\n".join(sections)
        blob, _ = scrub(blob, scrub_pii=self._scrub_pii)   # defense-in-depth
        suffix = "\n...(truncated)"
        if len(blob) > budget:
            blob = (blob[:max(0, budget - len(suffix))] + suffix)[:budget]   # provably within budget
        return blob
