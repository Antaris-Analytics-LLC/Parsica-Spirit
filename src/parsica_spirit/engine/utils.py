"""Shared utilities for the Parsica Spirit engine."""

import json
import os
import tempfile
import time as _time
import logging

logger = logging.getLogger("parsica_spirit.engine")


def atomic_write_json(path: str, data: dict, indent: int = 2, lock: bool = True) -> None:
    """Write JSON atomically with optional file locking.
    
    Prevents torn/partial writes from crashes or interrupted I/O.
    When lock=True (default), also prevents lost updates from concurrent writers
    using a directory-based lock.
    
    Args:
        path: File path to write
        data: JSON-serializable data
        indent: JSON indentation
        lock: If True, acquire a file lock before writing (default: True)
    """
    dir_path = os.path.dirname(path) or "."
    os.makedirs(dir_path, exist_ok=True)
    
    if lock:
        from .locking import FileLock
        with FileLock(path, timeout=30.0):
            _do_atomic_write(path, data, indent, dir_path)
    else:
        _do_atomic_write(path, data, indent, dir_path)


def _do_atomic_write(path: str, data, indent: int, dir_path: str) -> None:
    """Internal: perform the actual atomic write."""
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        # os.replace is atomic on all platforms, but on Windows it FAILS (PermissionError /
        # WinError 5/32) instead of blocking when another process has the destination open for
        # reading at that instant. That is transient under concurrent access, so retry with
        # backoff. POSIX rename-over-open succeeds on the first attempt.
        for _attempt in range(20):
            try:
                os.replace(tmp_path, path)
                break
            except PermissionError:
                if _attempt == 19:
                    raise
                _time.sleep(0.02 * (_attempt + 1))
        # Best-effort directory fsync for crash-consistent rename on POSIX
        try:
            dir_fd = os.open(dir_path, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except (OSError, AttributeError):
            pass  # Windows or unsupported - rename is still atomic
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def locked_read_json(path: str, default=None):
    """Read a JSON file under an exclusive directory lock to prevent torn reads.

    Note: Uses FileLock (exclusive mkdir-based lock), not a shared/read lock.
    This is intentional - it prevents concurrent writes from producing partial
    reads. Multiple simultaneous readers will serialize, which is acceptable
    for the low-read-frequency JSON files this function is designed for.
    
    Args:
        path: File path to read
        default: Value to return if file doesn't exist
        
    Returns:
        Parsed JSON data, or default if file doesn't exist.
    """
    if not os.path.exists(path):
        return default
    
    from .locking import FileLock
    with FileLock(path, timeout=10.0):
        with open(path) as f:
            return json.load(f)
