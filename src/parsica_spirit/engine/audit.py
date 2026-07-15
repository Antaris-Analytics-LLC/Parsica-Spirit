"""
Structured audit logging for the Parsica Spirit engine.

Append-only JSON Lines (.jsonl) format audit trail for all memory operations.
Thread-safe via file locking. Auto-rotation when files exceed size limit.
PII anonymization via HMAC-SHA256 with a per-instance or persistent salt.

Example usage:
    audit = AuditLogger("/path/to/workspace")
    
    # Log memory operations
    audit.log("ingest", {"id": "mem123", "source": "chat", "category": "technical"})
    audit.log("search", {"query": "Python tips", "results_count": 5, "duration_ms": 23})
    audit.log("recall", {"memory_id": "mem123", "context_id": "session_456"})
    
    # Query audit trail
    recent_recalls = audit.query(event_type="recall", since=time.time() - 3600)
    stats = audit.stats()
"""

import hashlib
import hmac
import json
import os
import re
import stat
import time
import threading
from pathlib import Path
from typing import Dict, List, Optional

try:
    import fcntl
    FCNTL_AVAILABLE = True
except ImportError:
    FCNTL_AVAILABLE = False

try:
    import msvcrt
    MSVCRT_AVAILABLE = True
except ImportError:
    MSVCRT_AVAILABLE = False

# Salt must be exactly 32 lowercase hex chars (128 bits from os.urandom(16).hex())
_SALT_RE = re.compile(r"^[0-9a-f]{32}$")


def _safe_chmod_600(path: Path) -> None:
    """Set file permissions to 0o600 (owner read+write only).
    
    Best-effort - silently ignores failures on Windows and restricted filesystems.
    """
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass  # Windows / restricted FS: best-effort only


def _read_or_create_salt(workspace: Path, initial_salt: str) -> str:
    """Load a persistent salt from .audit.salt, creating it if absent or corrupt.
    
    Validates that the stored salt is exactly 32 lowercase hex chars. If the file
    is missing, unreadable, or contains invalid data, writes initial_salt as the
    new salt and returns it.
    
    Args:
        workspace: Directory containing the .audit.salt file.
        initial_salt: Fresh salt to use if the file is absent or corrupt.
        
    Returns:
        A validated 32-char hex salt string.
    """
    workspace.mkdir(parents=True, exist_ok=True)
    salt_file = workspace / ".audit.salt"

    if salt_file.exists():
        try:
            s = salt_file.read_text(encoding="utf-8").strip()
        except Exception:
            s = ""
        if _SALT_RE.match(s):
            return s
        # Corrupt or invalid salt - fall through to regenerate

    # Write initial_salt (either first run or regeneration after corruption)
    try:
        salt_file.write_text(initial_salt, encoding="utf-8")
    except Exception:
        # If we can't write the salt file, return the in-memory salt without persisting
        return initial_salt
    _safe_chmod_600(salt_file)
    return initial_salt


class AuditLogger:
    """Append-only structured audit log for memory operations.
    
    Writes one JSON object per line to `<workspace>/.audit.jsonl`.
    Thread-safe via file locking. Rotation when file exceeds max_size_mb.
    
    PII anonymization uses HMAC-SHA256 with a per-instance salt (128 bits of
    entropy from os.urandom(16)). In persistent mode the salt is saved to
    .audit.salt (chmod 0o600). Validated on load - corrupted salt files are
    regenerated automatically.
    
    Events logged:
    - ingest: memory stored (id, source, category, timestamp)
    - search: query executed (query, results_count, duration_ms)
    - recall: memory retrieved and used (memory_id, context_id)
    - decay: memory score decayed (memory_id, old_score, new_score)
    - delete: memory removed (memory_id, reason)
    - reinforce: memory reinforced (memory_id, boost)
    """
    
    def __init__(
        self,
        workspace: str,
        max_size_mb: float = 10.0,
        max_files: int = 5,
        salt_mode: str = "ephemeral",
    ):
        """Initialize audit logger.
        
        Args:
            workspace: Directory path where audit logs will be stored
            max_size_mb: Maximum size of log file before rotation (default 10MB)
            max_files: Maximum number of rotated files to keep (default 5)
            salt_mode: Salt persistence mode - "ephemeral" (default, new salt each run)
                       or "persistent" (salt stored in .audit.salt, reused across runs)
        """
        self.workspace = Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)
        
        self.audit_file = self.workspace / ".audit.jsonl"
        self.max_size_bytes = int(max_size_mb * 1024 * 1024)
        self.max_files = max_files
        self._salt_mode = salt_mode
        
        # Thread safety
        self._lock = threading.Lock()
        
        # Per-instance salt for anonymization (prevents rainbow-table attacks).
        # os.urandom(16) = 128 bits of entropy - plenty for a MAC key.
        self._salt = os.urandom(16).hex()
        if salt_mode == "persistent":
            # Validate salt on load; regenerate if corrupt; chmod 0o600.
            # _read_or_create_salt ensures the salt file is always valid + restricted.
            self._salt = _read_or_create_salt(self.workspace, self._salt)
        
        # PII fields that should be hashed for anonymization
        self._pii_fields = {"content", "query", "text", "source"}
    
    def rotate_salt(self) -> None:
        """Generate a new salt, invalidating previous hashes.
        
        Persists to .audit.salt if salt_mode='persistent'. The new salt is validated
        via _read_or_create_salt so file write failures fall back to in-memory salt.
        """
        self._salt = os.urandom(16).hex()
        if self._salt_mode == "persistent":
            # Write + validate - re-uses the same hardened helper as __init__
            self._salt = _read_or_create_salt(self.workspace, self._salt)

    def log(self, event_type: str, data: dict) -> None:
        """Append a single audit event. Thread-safe.
        
        Args:
            event_type: Type of event (ingest, search, recall, etc.)
            data: Event-specific metadata dictionary. Written verbatim - pass metadata
                (ids, counts, hashes), not raw memory content or secrets; PII anonymization
                is a separate export-time step (anonymize_entry), not applied here.
        """
        entry = {
            "ts": time.time(),
            "event": event_type,
            "data": data
        }
        
        with self._lock:
            # Check if rotation is needed before writing
            if self.audit_file.exists() and self.audit_file.stat().st_size > self.max_size_bytes:
                self._rotate_logs()
            
            # Append the entry (atomic write)
            self._write_entry(entry)
    
    def _tail_lines(self, n: int) -> List[str]:
        """Read last n lines from audit file efficiently via backward seek.
        
        Decodes per-line (not per-buffer) to avoid silently corrupting
        JSON entries when a multibyte UTF-8 sequence is split across chunk boundaries.
        errors='replace' only applies to individual lines that are genuinely corrupt,
        not to the whole buffer.
        """
        if not self.audit_file.exists() or n <= 0:
            return []

        chunk_size = 8192
        lines: List[bytes] = []

        with open(self.audit_file, "rb") as f:
            f.seek(0, 2)
            remaining = f.tell()
            buf = b""
            while remaining > 0 and len(lines) < n + 1:
                read_size = min(chunk_size, remaining)
                remaining -= read_size
                f.seek(remaining)
                buf = f.read(read_size) + buf
                lines = buf.split(b"\n")

        # Decode each complete line individually - safe for any multibyte sequence
        out: List[str] = []
        for bline in lines:
            if not bline.strip():
                continue
            try:
                out.append(bline.decode("utf-8"))         # strict first
            except UnicodeDecodeError:
                out.append(bline.decode("utf-8", errors="replace"))  # fallback per-line
        return out[-n:] if len(out) >= n else out

    def query(
        self,
        event_type: str = None,
        since: float = None,
        limit: int = 100,
    ) -> List[dict]:
        """Query recent audit events. Reads from current file only.
        
        Fast path (since=None): tail-reads the last limit*3 lines. If event_type is
        specified and the fast path returns fewer than limit results, falls back to a
        full forward scan - this handles rare event types that may be older than the
        tail window. (Prevents silent under-return for infrequent events.)
        
        Slow path (since=<timestamp>): full forward scan with time filter.
        
        Args:
            event_type: Filter by event type (optional)
            since: Unix timestamp - only return events after this time (optional)
            limit: Maximum number of events to return (default 100)
            
        Returns:
            List of matching audit entries, newest first
        """
        if not self.audit_file.exists():
            return []
        
        from collections import deque
        with self._lock:
            if since is None:
                # Fast path: tail-read for recent events
                raw_lines = self._tail_lines(limit * 3)
                results = []
                for line in reversed(raw_lines):  # newest first
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        # Parse-then-filter - no string pre-filter.
                        # The previous `if event_type in line` check had false negatives
                        # when the event type appeared in data fields. Parsing 300 JSON
                        # lines is sub-millisecond on any modern hardware.
                        if event_type and entry.get("event") != event_type:
                            continue
                        results.append(entry)
                        if len(results) >= limit:
                            break
                    except json.JSONDecodeError:
                        continue

                # Fallback for rare event types older than the tail window.
                # Only triggered when event_type is set and fast path is incomplete.
                if event_type and len(results) < limit:
                    ring: deque = deque(maxlen=limit)
                    try:
                        with open(self.audit_file, "r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if not line:
                                    continue
                                try:
                                    entry = json.loads(line)
                                    if entry.get("event") != event_type:
                                        continue
                                    ring.append(entry)
                                except json.JSONDecodeError:
                                    continue
                    except IOError:
                        return results  # return fast-path partial results on I/O error
                    return list(reversed(ring))

                return results
            else:
                # Slow path: full forward scan for time-filtered queries
                ring = deque(maxlen=limit)
                try:
                    with open(self.audit_file, 'r', encoding='utf-8') as f:
                        for line in f:
                            line = line.strip()
                            if not line:
                                continue
                            # String pre-filter is safe in the slow path - we're scanning
                            # every line anyway; this early-exit reduces JSON parse calls
                            if event_type and (
                                f'"event": "{event_type}"' not in line
                                and f'"event":"{event_type}"' not in line
                            ):
                                continue
                            try:
                                entry = json.loads(line)
                                if entry.get("ts", 0) < since:
                                    continue
                                ring.append(entry)
                            except json.JSONDecodeError:
                                continue
                except IOError:
                    return []
                return list(reversed(ring))
    
    def rotate(self) -> None:
        """Rotate log file when it exceeds max_size_mb.
        
        Renames current file to .audit.1.jsonl, shifts others, creates new current file.
        """
        with self._lock:
            self._rotate_logs()
    
    def stats(self) -> dict:
        """Return audit statistics (counts per event type, file size, etc).
        
        Returns:
            Dictionary with statistics about the audit log
        """
        if not self.audit_file.exists():
            return {
                "total_entries": 0,
                "file_size_bytes": 0,
                "event_counts": {},
                "oldest_entry_ts": None,
                "newest_entry_ts": None,
            }
        
        event_counts = {}
        total_entries = 0
        oldest_ts = None
        newest_ts = None
        
        with self._lock:
            try:
                file_size = self.audit_file.stat().st_size
                
                with open(self.audit_file, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        
                        try:
                            entry = json.loads(line)
                            total_entries += 1
                            
                            # Count events by type
                            event_type = entry.get("event", "unknown")
                            event_counts[event_type] = event_counts.get(event_type, 0) + 1
                            
                            # Track timestamp range
                            ts = entry.get("ts")
                            if ts:
                                if oldest_ts is None or ts < oldest_ts:
                                    oldest_ts = ts
                                if newest_ts is None or ts > newest_ts:
                                    newest_ts = ts
                                    
                        except json.JSONDecodeError:
                            continue
                            
            except IOError:
                file_size = 0
        
        return {
            "total_entries": total_entries,
            "file_size_bytes": file_size,
            "event_counts": event_counts,
            "oldest_entry_ts": oldest_ts,
            "newest_entry_ts": newest_ts,
        }
    
    def anonymize_entry(self, entry: dict) -> dict:
        """Strip PII from an audit entry for safe export.
        Hashes memory content with HMAC-SHA256, preserves structure.
        
        Args:
            entry: Original audit entry dictionary
            
        Returns:
            New entry with PII fields replaced by HMAC-SHA256 hashes (32 hex chars / 128 bits)
        """
        # Deep copy to avoid modifying original
        anonymized = json.loads(json.dumps(entry))
        
        # Recursively anonymize PII fields
        self._anonymize_dict(anonymized)
        
        return anonymized
    
    def _anonymize_dict(self, obj: dict) -> None:
        """Recursively hash PII fields in a dictionary.
        
        Uses HMAC-SHA256 (correct MAC construction) and outputs 32 hex
        chars (128 bits) instead of the previous 16 chars (64 bits). HMAC prevents
        length-extension attacks and is the standard construction for keyed hashing.
        """
        for key, value in obj.items():
            if isinstance(value, dict):
                self._anonymize_dict(value)
            elif isinstance(value, str) and key.lower() in self._pii_fields:
                # HMAC-SHA256 with salt as key - correct keyed-hash construction
                digest = hmac.new(
                    key=bytes.fromhex(self._salt),
                    msg=value.encode("utf-8"),
                    digestmod=hashlib.sha256,
                ).hexdigest()
                obj[key] = f"hmac:{digest[:32]}"  # 32 hex = 128 bits
    
    def _write_entry(self, entry: dict) -> None:
        """Write a single entry to the log file with appropriate locking."""
        json_line = json.dumps(entry, ensure_ascii=False) + '\n'
        
        # Open in append mode for atomic writes
        with open(self.audit_file, 'a', encoding='utf-8') as f:
            if FCNTL_AVAILABLE:
                # Unix/Linux file locking
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    f.write(json_line)
                    f.flush()
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            else:
                # Windows or no-fcntl fallback: use directory-based FileLock
                # (consistent cross-OS, no byte-region issues with msvcrt.locking)
                from parsica_spirit.engine.locking import FileLock as _FileLock
                _lock = _FileLock(str(self.audit_file), timeout=10.0)
                with _lock:
                    # Re-open inside lock to get fresh file handle
                    with open(self.audit_file, 'a', encoding='utf-8') as _fw:
                        _fw.write(json_line)
                        _fw.flush()
                return  # already written inside lock
    
    def _rotate_logs(self) -> None:
        """Internal log rotation implementation.
        
        Wrapped in try/except OSError to handle Windows scenarios where
        antivirus/indexers hold files open, preventing rename operations.
        On failure, rotation is silently skipped and retried on next append.
        """
        if not self.audit_file.exists():
            return
        
        try:
            # Shift existing rotated files
            for i in range(self.max_files - 1, 0, -1):
                old_file = self.workspace / f".audit.{i}.jsonl"
                new_file = self.workspace / f".audit.{i + 1}.jsonl"
                
                if old_file.exists():
                    if new_file.exists():
                        new_file.unlink()  # Remove oldest file
                    old_file.rename(new_file)
            
            # Move current file to .1
            rotated_file = self.workspace / ".audit.1.jsonl"
            if rotated_file.exists():
                rotated_file.unlink()
            self.audit_file.rename(rotated_file)
        except OSError:
            # Rotation failed (e.g. file locked by another process on Windows).
            # Skip this cycle - rotation will retry on next log append.
            pass
        
        # Create new current file (will be created on next write)
