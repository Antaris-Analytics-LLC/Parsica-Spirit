"""
prune.py - Memory pruning engine for the Parsica Spirit engine.

Implements tiered pruning with dual-layer protection:
  1. Metadata gate  - importance score, access_count, recency scoring
  2. Content veto   - keyword hard-block for project/critical content

Tiers:
  small  - obvious junk only (pipeline fragments, <40 chars)
  medium - extends to low-signal noise (old zero-access entries, near-dupes)
  large  - aggressive cleanup (anything with low signal + no recent access)

Undo support:
  Every destructive prune writes a timestamped backup of deleted entries.
  /prune undo [timestamp] restores them.

Usage (standalone):
  python -m parsica_spirit.engine.prune --tier small --dry-run --memory-path ./store
  python -m parsica_spirit.engine.prune --tier medium --confirm --memory-path ./store
  python -m parsica_spirit.engine.prune undo --memory-path ./store
  python -m parsica_spirit.engine.prune undo 2026-03-05T23:15 --memory-path ./store
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional, Tuple

# -- Constants ------------------------------------------------------------------

MAX_BACKUPS = 5

# Turn-wrapper prefixes - content stored as "Turn: X" / "INPUT: X" is a wrapped
# fragment, not a useful memory on its own. Mirrors the engine's turn-prefix set.
PIPELINE_PREFIXES = (
    "INPUT:", "OUTPUT:", "Turn:", "TURN:", "System:",
)

# Content keyword hard-block - if any of these appear in entry content, NEVER prune
# regardless of metadata score. Covers project names, key identifiers, decisions.
# The name/identifier/infra entries ship as GENERIC EXAMPLES - replace and extend them
# with YOUR project names, service identifiers, and hosts. This list is the veto INPUT,
# not a tuned artifact; the signal groups below it are the reusable part.
PROTECTED_KEYWORDS = [
    # Project names (examples - put yours here)
    "parsica", "acme-agent", "acme-assistant",
    # Technical identifiers (examples - your services / repos)
    "parsica-spirit", "acme-suite", "acme-pipeline", "acme-guard",
    # Infrastructure (examples - your hosts / providers; IPs are RFC 5737 placeholders)
    "203.0.113.7", "198.51.100.42", "pypi", "github",
    # Versioning signals
    "v4.", "v3.", "v2.", "v1.", "v5.", "version",
    # Decision/planning signals
    "roadmap", "milestone", "decision", "decided", "shipped", "deployed",
    "rule #", "rule:", "never ", "always ", "critical",
    # Enrichment signals - entries with enrichment are high value
    "enriched_summary", "search_keywords",
    # API / credential signals
    "sk-ant", "sk-proj", "api key", "token", "password",
    # URL signals
    "https://", "http://",
]

# Compiled for speed
_PROTECTED_RE = re.compile(
    "|".join(re.escape(kw) for kw in PROTECTED_KEYWORDS),
    re.IGNORECASE,
)


# -- Data classes ---------------------------------------------------------------

@dataclass
class PruneCandidate:
    entry: dict          # raw entry dict from shard
    shard_path: str      # which shard file it came from
    reason: str          # why it was flagged
    safety_score: float  # 0.0 = very safe to delete, 1.0 = risky


@dataclass
class PruneResult:
    removed: int = 0
    kept: int = 0
    total_scanned: int = 0
    backup_path: Optional[str] = None
    samples_removed: List[dict] = field(default_factory=list)
    samples_kept: List[dict] = field(default_factory=list)
    dry_run: bool = True
    tier: str = "small"
    errors: List[str] = field(default_factory=list)


# -- Scoring --------------------------------------------------------------------

def _content_is_protected(content: str) -> bool:
    """Return True if content contains any protected keyword -> never prune."""
    return bool(_PROTECTED_RE.search(content))


def _has_enrichment(entry: dict) -> bool:
    """Return True if entry has LLM enrichment data - high signal, protect."""
    return bool(entry.get("enriched_summary") or entry.get("search_keywords") or entry.get("search_queries"))


def _entry_age_days(entry: dict) -> float:
    """Days since entry was created."""
    try:
        created = datetime.fromisoformat(entry.get("created", ""))
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if created.tzinfo:
            created = created.replace(tzinfo=None)
        return (now - created).total_seconds() / 86400
    except Exception:
        return 0.0


def _last_access_days(entry: dict) -> float:
    """Days since entry was last accessed."""
    try:
        last = datetime.fromisoformat(entry.get("last_accessed", entry.get("created", "")))
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if last.tzinfo:
            last = last.replace(tzinfo=None)
        return (now - last).total_seconds() / 86400
    except Exception:
        return 0.0


def _safety_score(entry: dict) -> float:
    """
    Compute a 'safety to delete' score: 0.0 = very safe, 1.0 = risky/protect.
    Higher score = keep. Lower score = candidate for deletion.
    """
    score = 0.0

    # Access count - any access at all is a strong keep signal
    access = entry.get("access_count", 0)
    if access > 5:
        score += 0.5
    elif access > 0:
        score += 0.3

    # Importance - high importance = keep
    importance = entry.get("importance", 1.0)
    score += importance * 0.3

    # Recency - recently accessed = keep
    last_access = _last_access_days(entry)
    if last_access < 7:
        score += 0.4
    elif last_access < 30:
        score += 0.2

    # Enrichment - enriched entries are high value
    if _has_enrichment(entry):
        score += 0.4

    # Tags - tagged entries have structured signal
    if entry.get("tags"):
        score += 0.1

    # Memory type - non-episodic types are usually more important
    if entry.get("memory_type", "episodic") != "episodic":
        score += 0.2

    return min(score, 1.0)


# -- Candidate detection --------------------------------------------------------

def _is_pipeline_fragment(content: str) -> bool:
    return any(content.strip().startswith(p) for p in PIPELINE_PREFIXES)


def _is_too_short(content: str, min_len: int = 40) -> bool:
    return len(content.strip()) < min_len


def _find_candidates_small(entries: List[dict], shard_path: str) -> List[PruneCandidate]:
    """
    Small tier: obvious junk only.
    - Pipeline fragments (INPUT:, OUTPUT:, Turn: ...)
    - Entries < 40 chars
    - Zero-access entries < 7 days old with importance < 0.2
    """
    candidates = []
    for entry in entries:
        content = entry.get("content", "")

        if _is_pipeline_fragment(content):
            candidates.append(PruneCandidate(entry, shard_path, "pipeline_fragment", 0.05))
        elif _is_too_short(content):
            candidates.append(PruneCandidate(entry, shard_path, "too_short", 0.1))
        elif (entry.get("access_count", 0) == 0
              and entry.get("importance", 1.0) < 0.2
              and _entry_age_days(entry) < 7):
            candidates.append(PruneCandidate(entry, shard_path, "low_signal_fresh", 0.15))

    return candidates


def _find_candidates_medium(entries: List[dict], shard_path: str) -> List[PruneCandidate]:
    """
    Medium tier: small + low-signal noise.
    - Everything in small
    - Zero-access entries older than 30 days with importance < 0.4
    - Generic filler content (greetings, one-word replies)
    """
    candidates = _find_candidates_small(entries, shard_path)
    flagged_hashes = {c.entry.get("hash") for c in candidates}

    FILLER_PATTERNS = [
        re.compile(r"^(ok|okay|got it|sure|yes|no|thanks|thank you|noted|understood|acknowledged)[.!]?\s*$", re.IGNORECASE),
        re.compile(r"^(standing by|ready|on it|will do)[.!]?\s*$", re.IGNORECASE),
    ]

    for entry in entries:
        if entry.get("hash") in flagged_hashes:
            continue
        content = entry.get("content", "")

        if (entry.get("access_count", 0) == 0
                and entry.get("importance", 1.0) < 0.4
                and _entry_age_days(entry) > 30):
            candidates.append(PruneCandidate(entry, shard_path, "old_zero_access", 0.2))
        elif any(p.match(content.strip()) for p in FILLER_PATTERNS):
            candidates.append(PruneCandidate(entry, shard_path, "filler_content", 0.15))

    return candidates


def _find_candidates_large(entries: List[dict], shard_path: str) -> List[PruneCandidate]:
    """
    Large tier: medium + aggressive low-signal cleanup.
    - Everything in medium
    - Zero-access entries older than 14 days with importance < 0.6
    - Any entry with importance < 0.3 and last_access > 60 days
    """
    candidates = _find_candidates_medium(entries, shard_path)
    flagged_hashes = {c.entry.get("hash") for c in candidates}

    for entry in entries:
        if entry.get("hash") in flagged_hashes:
            continue

        if (entry.get("access_count", 0) == 0
                and entry.get("importance", 1.0) < 0.6
                and _entry_age_days(entry) > 14):
            candidates.append(PruneCandidate(entry, shard_path, "low_signal_stale", 0.3))
        elif (entry.get("importance", 1.0) < 0.3
              and _last_access_days(entry) > 60):
            candidates.append(PruneCandidate(entry, shard_path, "very_stale_low_importance", 0.35))

    return candidates


TIER_FINDERS = {
    "small": _find_candidates_small,
    "medium": _find_candidates_medium,
    "large": _find_candidates_large,
}


# -- Shard I/O ------------------------------------------------------------------

def _load_shard(path: str) -> List[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Support both "entries" (legacy) and "memories" (current format)
        return data.get("memories", data.get("entries", []))
    except Exception:
        return []


def _save_shard(path: str, entries: List[dict]):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}
    # Preserve the original key name ("memories" vs "entries")
    key = "memories" if "memories" in data else "entries"
    data[key] = entries
    data["count"] = len(entries)
    data["saved_at"] = datetime.now().isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _find_shards(memory_path: str) -> List[str]:
    p = Path(memory_path)
    if not p.exists():
        return []
    # Check direct shards (legacy) and shards/ subdirectory (current)
    direct = sorted(p.glob("shard_*.json"))
    subdir = sorted((p / "shards").glob("shard_*.json")) if (p / "shards").exists() else []
    return [str(f) for f in (direct + subdir)]


# -- Backup / Undo --------------------------------------------------------------

def _backup_dir(memory_path: str) -> Path:
    return Path(memory_path).parent / "prune-backups"


def _write_backup(memory_path: str, deleted_entries: List[dict], tier: str) -> str:
    """Write a timestamped backup of deleted entries. Returns backup path."""
    backup_dir = _backup_dir(memory_path)
    backup_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    backup_path = backup_dir / f"prune-{ts}-{tier}.json"

    with open(backup_path, "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": ts,
            "tier": tier,
            "memory_path": str(memory_path),
            "count": len(deleted_entries),
            "entries": deleted_entries,
        }, f, indent=2, ensure_ascii=False)

    # Enforce retention: keep only last MAX_BACKUPS
    all_backups = sorted(backup_dir.glob("prune-*.json"))
    while len(all_backups) > MAX_BACKUPS:
        oldest = all_backups.pop(0)
        oldest.unlink(missing_ok=True)

    return str(backup_path)


def list_backups(memory_path: str) -> List[dict]:
    """List available prune backups."""
    backup_dir = _backup_dir(memory_path)
    if not backup_dir.exists():
        return []
    backups = []
    for f in sorted(backup_dir.glob("prune-*.json"), reverse=True):
        try:
            with open(f, encoding="utf-8") as fp:
                data = json.load(fp)
            backups.append({
                "path": str(f),
                "timestamp": data.get("timestamp", "?"),
                "tier": data.get("tier", "?"),
                "count": data.get("count", 0),
            })
        except Exception:
            pass
    return backups


def undo_prune(memory_path: str, timestamp: Optional[str] = None) -> dict:
    """Restore deleted entries from a backup. If timestamp=None, uses latest."""
    backup_dir = _backup_dir(memory_path)
    if not backup_dir.exists():
        return {"ok": False, "error": "No prune backups found"}

    backups = sorted(backup_dir.glob("prune-*.json"), reverse=True)
    if not backups:
        return {"ok": False, "error": "No prune backups found"}

    if timestamp:
        # Find backup matching timestamp prefix
        ts_clean = timestamp.replace(":", "-").replace("T", "T")
        matched = [b for b in backups if ts_clean in b.name]
        if not matched:
            return {"ok": False, "error": f"No backup found matching '{timestamp}'"}
        backup_file = matched[0]
    else:
        backup_file = backups[0]

    try:
        with open(backup_file, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return {"ok": False, "error": f"Could not read backup: {e}"}

    entries_to_restore = data.get("entries", [])
    if not entries_to_restore:
        return {"ok": False, "error": "Backup is empty"}

    # Group by original shard or use default shard
    shards = _find_shards(memory_path)
    if not shards:
        return {"ok": False, "error": f"No shards found at {memory_path}"}

    # Re-ingest entries back into the appropriate shard (use first shard as fallback)
    # Group entries by their date key if possible
    from collections import defaultdict
    shard_map: dict = defaultdict(list)
    for entry in entries_to_restore:
        created = entry.get("created", "")
        date_key = created[:7] if created else "unknown"  # YYYY-MM
        shard_map[date_key].append(entry)

    restored = 0
    for shard_path in shards:
        shard_name = Path(shard_path).name  # shard_YYYY-MM_topic.json
        parts = shard_name.replace(".json", "").split("_")
        date_key = parts[1] if len(parts) > 1 else ""
        if date_key in shard_map:
            existing = _load_shard(shard_path)
            existing_hashes = {e.get("hash") for e in existing}
            to_add = [e for e in shard_map[date_key] if e.get("hash") not in existing_hashes]
            if to_add:
                existing.extend(to_add)
                _save_shard(shard_path, existing)
                restored += len(to_add)
                del shard_map[date_key]

    # Any remaining entries go to the most recent shard
    remaining = [e for entries in shard_map.values() for e in entries]
    if remaining:
        target_shard = shards[-1]
        existing = _load_shard(target_shard)
        existing_hashes = {e.get("hash") for e in existing}
        to_add = [e for e in remaining if e.get("hash") not in existing_hashes]
        existing.extend(to_add)
        _save_shard(target_shard, existing)
        restored += len(to_add)

    # Delete the backup file after successful restore
    backup_file.unlink(missing_ok=True)

    return {
        "ok": True,
        "restored": restored,
        "backup": str(backup_file),
    }


# -- Main prune engine ----------------------------------------------------------

def prune(
    memory_path: str,
    tier: str = "small",
    dry_run: bool = True,
    confirm: bool = False,
) -> PruneResult:
    """
    Run a prune pass over all shards.

    Args:
        memory_path: path to the memory store directory
        tier: "small", "medium", or "large"
        dry_run: if True, only report what would be deleted (no changes)
        confirm: must be True (along with dry_run=False) to actually delete

    Returns:
        PruneResult with counts, samples, and backup path (if not dry_run)
    """
    if tier not in TIER_FINDERS:
        raise ValueError(f"Unknown tier '{tier}'. Choose: small, medium, large")

    finder = TIER_FINDERS[tier]
    shards = _find_shards(memory_path)

    result = PruneResult(dry_run=dry_run, tier=tier)

    if not shards:
        result.errors.append(f"No shards found at {memory_path}")
        return result

    all_candidates: List[PruneCandidate] = []
    shard_entry_map: dict = {}  # shard_path -> [entries]

    # Scan all shards and collect candidates
    for shard_path in shards:
        entries = _load_shard(shard_path)
        shard_entry_map[shard_path] = entries
        result.total_scanned += len(entries)

        candidates = finder(entries, shard_path)

        # Apply dual-layer protection: veto any candidate with protected content
        safe_candidates = []
        for c in candidates:
            content = c.entry.get("content", "")
            # Layer 1: Content keyword hard block - if the content itself mentions
            # protected project terms, never delete regardless of reason
            if _content_is_protected(content):
                continue
            # Layer 2: Metadata gate - skip entries with meaningful signal
            # EXCEPT for clearly junk reasons (pipeline_fragment, too_short)
            # which bypass enrichment/access_count checks since LLM enrichment
            # was applied to the junk content, not to real signal
            if c.reason not in ("pipeline_fragment", "too_short"):
                if _has_enrichment(c.entry):
                    continue
                if _safety_score(c.entry) > 0.5:
                    continue
            else:
                # Even for junk reasons: still check score if access is very high
                # (accessed 20+ times means it's somehow being recalled in search)
                if c.entry.get("access_count", 0) > 20 and _safety_score(c.entry) > 0.7:
                    continue
            safe_candidates.append(c)

        all_candidates.extend(safe_candidates)

    result.removed = len(all_candidates)
    result.kept = result.total_scanned - result.removed

    # Collect samples for reporting (up to 5 per reason category)
    reason_counts: dict = {}
    for c in all_candidates:
        reason_counts[c.reason] = reason_counts.get(c.reason, 0) + 1

    seen_reasons: dict = {}
    for c in all_candidates:
        if seen_reasons.get(c.reason, 0) < 3:
            result.samples_removed.append({
                "hash": c.entry.get("hash", "?"),
                "reason": c.reason,
                "content_preview": c.entry.get("content", "")[:120],
                "access_count": c.entry.get("access_count", 0),
                "importance": c.entry.get("importance", 1.0),
                "age_days": round(_entry_age_days(c.entry), 1),
            })
            seen_reasons[c.reason] = seen_reasons.get(c.reason, 0) + 1

    result.samples_removed = result.samples_removed[:15]  # cap total samples

    if dry_run or not confirm:
        return result

    # -- Perform actual deletion ------------------------------------------------
    candidate_hashes = {c.entry.get("hash") for c in all_candidates}
    deleted_entries = [c.entry for c in all_candidates]

    # Write backup BEFORE deletion
    result.backup_path = _write_backup(memory_path, deleted_entries, tier)

    # Remove candidates from each shard and save
    for shard_path, entries in shard_entry_map.items():
        new_entries = [e for e in entries if e.get("hash") not in candidate_hashes]
        if len(new_entries) != len(entries):
            _save_shard(shard_path, new_entries)

    return result


# -- CLI ------------------------------------------------------------------------

def _format_result(result: PruneResult) -> str:
    lines = []
    mode = "DRY RUN" if result.dry_run else "PRUNED"
    lines.append(f"\n{'='*50}")
    lines.append(f"  /prune {result.tier.upper()} - {mode}")
    lines.append(f"{'='*50}")
    lines.append(f"  Scanned:  {result.total_scanned:,} entries")
    lines.append(f"  {'Would remove' if result.dry_run else 'Removed'}:  {result.removed:,} entries")
    lines.append(f"  Kept:     {result.kept:,} entries")
    if result.backup_path:
        lines.append(f"  Backup:   {result.backup_path}")
    lines.append("")

    if result.samples_removed:
        lines.append("  Sample entries that would be removed:")
        for s in result.samples_removed[:8]:
            preview = s["content_preview"].replace("\n", " ")
            lines.append(f"    [{s['reason']}] age={s['age_days']}d access={s['access_count']} -> {preview[:80]}")

    if result.errors:
        lines.append("\n  Errors:")
        for e in result.errors:
            lines.append(f"    ! {e}")

    lines.append("")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="parsica-spirit prune tool")
    parser.add_argument("action", nargs="?", default="prune",
                        help="prune or undo")
    parser.add_argument("--tier", choices=["small", "medium", "large"], default="small",
                        help="Prune tier (default: small)")
    parser.add_argument("--memory-path", default="./parsica_spirit.engine_store",
                        help="Path to memory store")
    parser.add_argument("--dry-run", action="store_true", default=True,
                        help="Preview only, no changes (default: on)")
    parser.add_argument("--confirm", action="store_true", default=False,
                        help="Actually perform deletion (disables dry-run)")
    parser.add_argument("--timestamp", default=None,
                        help="For undo: timestamp of backup to restore")
    parser.add_argument("--list-backups", action="store_true",
                        help="List available prune backups")
    parser.add_argument("--json", action="store_true",
                        help="Output JSON instead of human-readable")

    args = parser.parse_args()

    if args.list_backups:
        backups = list_backups(args.memory_path)
        if args.json:
            print(json.dumps({"ok": True, "backups": backups}))
        else:
            if not backups:
                print("No prune backups found.")
            else:
                print(f"\nAvailable prune backups ({args.memory_path}):")
                for b in backups:
                    print(f"  {b['timestamp']}  tier={b['tier']}  entries={b['count']}")
        return

    if args.action == "undo":
        r = undo_prune(args.memory_path, args.timestamp)
        if args.json:
            print(json.dumps(r))
        else:
            if r["ok"]:
                print(f"\nRestored {r['restored']} entries from {r['backup']}")
            else:
                print(f"\nUndo failed: {r['error']}")
        return

    # Default: prune
    dry_run = not args.confirm
    r = prune(args.memory_path, tier=args.tier, dry_run=dry_run, confirm=args.confirm)
    if args.json:
        print(json.dumps({
            "ok": True,
            "removed": r.removed,
            "kept": r.kept,
            "total_scanned": r.total_scanned,
            "dry_run": r.dry_run,
            "tier": r.tier,
            "backup_path": r.backup_path,
            "samples": r.samples_removed,
            "errors": r.errors,
        }))
    else:
        print(_format_result(r))
        if dry_run and r.removed > 0:
            print(f"  Run with --confirm to apply. A backup will be created automatically.\n")


if __name__ == "__main__":
    main()
