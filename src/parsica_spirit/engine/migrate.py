"""
Migration utility for the Parsica Spirit engine.

Migrates a flat ``memories.jsonl`` store to time-window shard files
(``memories_YYYY-WXX.jsonl``) without losing any data.

Safety contract
---------------
* Reads the entire flat file first.
* Writes all entries to shard files.
* Only after all writes succeed, copies the original to ``memories.jsonl.bak``.
* The original is never deleted - ``memories.jsonl.bak`` is the backup.
* Calling ``migrate_to_shards()`` on a workspace that has already been
  migrated is a no-op (idempotent).

Usage::

    from parsica_spirit.engine.migrate import migrate_to_shards

    result = migrate_to_shards("/path/to/workspace")
    # {"status": "success", "entries_migrated": 412, "shards": [...], "backup": "..."}
"""

import json
import os
import shutil
from typing import Dict, List

from .entry import MemoryEntry
from .shards import ShardManager


def migrate_to_shards(workspace: str) -> Dict:
    """Migrate a flat ``memories.jsonl`` file to time-window shard files.

    Parameters
    ----------
    workspace:
        Root workspace directory containing ``memories.jsonl``.

    Returns
    -------
    dict with keys:

    ``status``
        ``"success"``, ``"skipped"``, or ``"already_migrated"``.
    ``reason`` *(when skipped)*
        Human-readable explanation.
    ``entries_migrated`` *(when success)*
        Number of entries written to shard files.
    ``shards`` *(when success)*
        List of shard filenames created or updated.
    ``backup`` *(when success)*
        Path to the backup file (``memories.jsonl.bak``).
    """
    flat_path = os.path.join(workspace, "memories.jsonl")
    backup_path = flat_path + ".bak"

    # -- guard: flat file must exist ----------------------------------------
    if not os.path.exists(flat_path):
        return {
            "status": "skipped",
            "reason": "memories.jsonl not found in workspace",
        }

    # -- guard: idempotency - if shards already exist, skip -----------------
    mgr = ShardManager(workspace)
    existing_shards = mgr.list_shards()
    if existing_shards:
        return {
            "status": "already_migrated",
            "reason": f"{len(existing_shards)} shard(s) already present",
            "shards": existing_shards,
        }

    # -- read all entries from flat file ------------------------------------
    entries: List[MemoryEntry] = []
    errors = 0
    try:
        with open(flat_path, "r", encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, start=1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    d = json.loads(stripped)
                    entries.append(MemoryEntry.from_dict(d))
                except Exception:
                    errors += 1  # corrupted line - skip, keep count
    except OSError as exc:
        return {
            "status": "skipped",
            "reason": f"could not read memories.jsonl: {exc}",
        }

    if not entries:
        return {
            "status": "skipped",
            "reason": "no valid entries found in memories.jsonl",
        }

    # -- write all entries to shard files ----------------------------------
    for entry in entries:
        mgr.write_entry(entry)

    # -- confirm migration was complete before creating backup --------------
    written_count = sum(
        _count_lines(os.path.join(workspace, s)) for s in mgr.list_shards()
    )
    # We expect at least as many lines as entries (duplicates in the flat file
    # will each have been written once).
    if written_count < len(entries):
        return {
            "status": "error",
            "reason": (
                f"shard write count ({written_count}) < entries read "
                f"({len(entries)}); migration may be incomplete"
            ),
        }

    # -- back up the original ---------------------------------------------
    shutil.copy2(flat_path, backup_path)

    return {
        "status": "success",
        "entries_migrated": len(entries),
        "entries_skipped": errors,
        "shards": mgr.list_shards(),
        "backup": backup_path,
    }


def _count_lines(path: str) -> int:
    """Count non-empty lines in a file."""
    if not os.path.exists(path):
        return 0
    count = 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    count += 1
    except OSError:
        pass
    return count
