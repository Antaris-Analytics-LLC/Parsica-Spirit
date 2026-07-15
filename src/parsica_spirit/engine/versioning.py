"""
Optimistic conflict detection for read-modify-write patterns.

Tracks file modification times and content hashes to detect when another
process has modified a file between your read and write. Prevents silent
data loss from concurrent modifications.

Usage:
    tracker = VersionTracker()
    
    # Record state when you read a file
    version = tracker.snapshot("/path/to/data.json")
    
    # ... modify data ...
    
    # Check before writing - raises if file changed since snapshot
    tracker.check(version)  # raises ConflictError if modified
    atomic_write_json("/path/to/data.json", new_data)
    
    # Or use the safe_update helper:
    def modify(data):
        data["count"] += 1
        return data
    
    tracker.safe_update("/path/to/data.json", modify)
"""

import hashlib
import json
import os
import time
import logging
from typing import Callable, Optional, Any

logger = logging.getLogger("parsica_spirit.engine")


class ConflictError(Exception):
    """Raised when a file has been modified since it was read."""
    
    def __init__(self, path: str, expected_mtime: float, actual_mtime: float):
        self.path = path
        self.expected_mtime = expected_mtime
        self.actual_mtime = actual_mtime
        super().__init__(
            f"Conflict detected on {os.path.basename(path)}: "
            f"file modified since last read "
            f"(expected mtime={expected_mtime:.6f}, actual={actual_mtime:.6f})"
        )


class FileVersion:
    """Snapshot of a file's state at a point in time."""
    
    __slots__ = ("path", "mtime", "size", "content_hash", "taken_at")
    
    def __init__(self, path: str, mtime: float, size: int, 
                 content_hash: str, taken_at: float):
        self.path = path
        self.mtime = mtime
        self.size = size
        self.content_hash = content_hash
        self.taken_at = taken_at
    
    def is_current(self) -> bool:
        """Check if the file still matches this snapshot."""
        try:
            stat = os.stat(self.path)
            if stat.st_mtime != self.mtime:
                return False
            if stat.st_size != self.size:
                return False
            return True
        except OSError:
            return False


class VersionTracker:
    """Tracks file versions for optimistic conflict detection.
    
    Lightweight alternative to full locking for read-heavy workloads
    where writes are infrequent and conflicts are rare.
    """
    
    def __init__(self, use_content_hash: bool = False):
        """
        Args:
            use_content_hash: If True, also compute SHA-256 of file content
                for stronger conflict detection. Slower but catches
                same-mtime-different-content edge cases.
        """
        self.use_content_hash = use_content_hash
    
    def snapshot(self, path: str) -> FileVersion:
        """Take a snapshot of a file's current state.
        
        Args:
            path: Path to the file
            
        Returns:
            FileVersion capturing the file's mtime, size, and optionally content hash.
        """
        stat = os.stat(path)
        
        content_hash = ""
        if self.use_content_hash:
            with open(path, "rb") as f:
                content_hash = hashlib.sha256(f.read()).hexdigest()
        
        return FileVersion(
            path=path,
            mtime=stat.st_mtime,
            size=stat.st_size,
            content_hash=content_hash,
            taken_at=time.time(),
        )
    
    def check(self, version: FileVersion) -> None:
        """Verify a file hasn't changed since the snapshot.
        
        Args:
            version: A FileVersion from a previous snapshot() call
            
        Raises:
            ConflictError: If the file has been modified
            FileNotFoundError: If the file was deleted
        """
        try:
            stat = os.stat(version.path)
        except FileNotFoundError:
            raise ConflictError(version.path, version.mtime, 0)
        
        if stat.st_mtime != version.mtime or stat.st_size != version.size:
            raise ConflictError(version.path, version.mtime, stat.st_mtime)
        
        if self.use_content_hash and version.content_hash:
            with open(version.path, "rb") as f:
                current_hash = hashlib.sha256(f.read()).hexdigest()
            if current_hash != version.content_hash:
                raise ConflictError(version.path, version.mtime, stat.st_mtime)
    
    def safe_update(self, path: str, modifier: Callable[[Any], Any],
                    max_retries: int = 3) -> Any:
        """Read-modify-write with automatic retry on conflict.
        
        Args:
            path: Path to JSON file
            modifier: Function that takes parsed JSON data and returns modified data
            max_retries: Number of retry attempts on conflict
            
        Returns:
            The modified data that was written
            
        Raises:
            ConflictError: If all retries exhausted
        """
        from .utils import atomic_write_json
        
        for attempt in range(max_retries + 1):
            version = self.snapshot(path)
            
            with open(path) as f:
                data = json.load(f)
            
            modified = modifier(data)
            
            try:
                self.check(version)
            except ConflictError:
                if attempt < max_retries:
                    logger.warning(
                        f"Conflict on {os.path.basename(path)}, "
                        f"retry {attempt + 1}/{max_retries}"
                    )
                    time.sleep(0.01 * (attempt + 1))  # Brief backoff
                    continue
                raise
            
            atomic_write_json(path, modified)
            return modified
        
        raise ConflictError(path, 0, 0)  # Should not reach here
