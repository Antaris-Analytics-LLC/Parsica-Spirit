"""
Cross-platform file locking using lock directories.

Uses os.mkdir() for atomic lock acquisition - portable across POSIX and Windows,
works on network filesystems, zero dependencies.

Usage:
    lock = FileLock("/path/to/resource.json")
    with lock:
        data = load(resource)
        modify(data)
        save(resource, data)

    # Or with timeout:
    with FileLock("/path/to/resource.json", timeout=5.0):
        ...

    # Non-blocking try:
    lock = FileLock("/path/to/resource.json")
    if lock.acquire(blocking=False):
        try:
            ...
        finally:
            lock.release()
"""

import os
import time
import json
import logging

logger = logging.getLogger("parsica_spirit.engine")

# Stale lock threshold - if a lock is older than this, assume the holder crashed
STALE_LOCK_SECONDS = 300  # 5 minutes


def _pid_is_dead(pid: int) -> bool:
    """True ONLY if the PID provably does not exist. Conservative on both platforms: any ambiguity
    (access denied, cannot query) returns False (treat as alive), so a LIVE holder's lock is never
    broken. On POSIX uses os.kill(pid, 0); on Windows uses OpenProcess + GetExitCodeProcess
    (never os.kill, which on Windows TERMINATES the process instead of probing it)."""
    if pid is None:
        return False
    if os.name == "posix":
        try:
            os.kill(pid, 0)
            return False
        except ProcessLookupError:
            return True
        except OSError:
            return False  # exists but not signalable (permissions) -> alive
    try:
        import ctypes
        from ctypes import wintypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        ERROR_INVALID_PARAMETER = 87
        STILL_ACTIVE = 259
        k = ctypes.windll.kernel32
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return k.GetLastError() == ERROR_INVALID_PARAMETER  # no such PID == dead; else assume alive
        try:
            code = wintypes.DWORD()
            if k.GetExitCodeProcess(h, ctypes.byref(code)):
                return code.value != STILL_ACTIVE
            return False
        finally:
            k.CloseHandle(h)
    except Exception:
        return False  # never break a live lock on a probe error


class LockTimeout(Exception):
    """Raised when a lock cannot be acquired within the timeout."""
    pass


class FileLock:
    """Cross-platform file lock using mkdir().
    
    os.mkdir() is atomic on all major platforms and filesystems.
    The lock directory contains a metadata file with holder info
    for debugging stale locks.
    
    Args:
        path: Path to the resource being locked (the .lock dir is derived from this)
        timeout: Maximum seconds to wait for lock acquisition (None = wait forever)
        poll_interval: Seconds between acquisition attempts
        stale_threshold: Seconds after which an unrefreshed lock is considered stale
    """
    
    def __init__(self, path: str, timeout: float = 30.0, 
                 poll_interval: float = 0.05, stale_threshold: float = STALE_LOCK_SECONDS):
        self.path = path
        self.lock_dir = path + ".lock"
        self.meta_path = os.path.join(self.lock_dir, "holder.json")
        self.timeout = timeout
        self.poll_interval = poll_interval
        self.stale_threshold = stale_threshold
        self._held = False
    
    def acquire(self, blocking: bool = True,
                timeout: float = None,
                stale_timeout: float = None) -> bool:
        """Acquire the lock, optionally overriding instance-level timeouts.

        *stale_timeout* lets callers specify a per-call stale
        threshold (seconds) that overrides ``self.stale_threshold``.  A lock
        is considered stale when its holder PID is no longer running **or**
        when the lock is older than *stale_timeout* seconds.

        Args:
            blocking: If True, wait up to *timeout* (or ``self.timeout``).
                If False, return immediately.
            timeout: Per-call acquisition timeout in seconds.  If ``None``,
                ``self.timeout`` is used.
            stale_timeout: Per-call stale threshold in seconds.  If ``None``,
                ``self.stale_threshold`` is used.

        Returns:
            True if lock acquired, False if non-blocking and lock unavailable.

        Raises:
            LockTimeout: If blocking and timeout exceeded.
        """
        eff_timeout = timeout if timeout is not None else self.timeout
        eff_stale = stale_timeout if stale_timeout is not None else self.stale_threshold
        start = time.monotonic()

        while True:
            try:
                os.mkdir(self.lock_dir)
                # Lock acquired - write holder metadata
                self._write_meta()
                self._held = True
                logger.debug(f"Lock acquired: {self.lock_dir}")
                return True
            except OSError:
                # Lock directory exists - check if stale
                if self._break_stale(stale_threshold=eff_stale):
                    continue  # Stale lock broken, retry immediately

                if not blocking:
                    return False

                elapsed = time.monotonic() - start
                if eff_timeout is not None and elapsed >= eff_timeout:
                    raise LockTimeout(
                        f"Could not acquire lock on {self.path} "
                        f"after {eff_timeout:.1f}s (holder: {self._read_holder()})"
                    )

                time.sleep(self.poll_interval)
    
    def release(self):
        """Release the lock."""
        if not self._held:
            return
        
        try:
            # Remove metadata file first, then lock directory
            if os.path.exists(self.meta_path):
                os.unlink(self.meta_path)
            os.rmdir(self.lock_dir)
            logger.debug(f"Lock released: {self.lock_dir}")
        except OSError as e:
            logger.warning(f"Error releasing lock {self.lock_dir}: {e}")
        finally:
            self._held = False
    
    def _write_meta(self):
        """Write lock holder metadata for debugging.

        uses ISO8601 timestamp and includes ``acquired_at_ts``
        (Unix float) for fast age comparisons without parsing.
        """
        try:
            now_ts = time.time()
            from datetime import datetime, timezone
            meta = {
                "pid": os.getpid(),
                "acquired_at": datetime.fromtimestamp(
                    now_ts, tz=timezone.utc
                ).isoformat(),
                "acquired_at_ts": now_ts,   # kept for fast arithmetic
                "path": self.path,
            }
            with open(self.meta_path, "w") as f:
                json.dump(meta, f)
        except OSError:
            pass  # Non-critical - lock is held regardless
    
    def _read_holder(self) -> str:
        """Read holder info for error messages."""
        try:
            with open(self.meta_path) as f:
                meta = json.load(f)
            return f"pid={meta.get('pid')}, acquired={meta.get('acquired_at')}"
        except (OSError, json.JSONDecodeError, KeyError):
            return "unknown"
    
    def _break_stale(self, stale_threshold: float = None) -> bool:
        """Break a stale lock if the holder appears to have crashed.

        accepts an optional *stale_threshold* override.

        A lock is considered stale if:

        1. The holder.json is older than *stale_threshold* seconds, OR
        2. The holder.json doesn't exist (incomplete lock), OR
        3. The holder PID is no longer running (POSIX only) - checked
           *before* the age check so crashed-process locks are broken
           immediately regardless of age.

        Returns True if stale lock was broken.
        """
        threshold = stale_threshold if stale_threshold is not None else self.stale_threshold
        try:
            if not os.path.exists(self.meta_path):
                # Lock dir exists but no metadata - likely crashed during acquire
                lock_age = time.time() - os.path.getmtime(self.lock_dir)
                if lock_age > threshold:
                    self._force_break()
                    return True
                return False

            with open(self.meta_path) as f:
                meta = json.load(f)

            holder_pid = meta.get("pid")

            # Check if holder PID is alive - takes priority over age. Cross-platform and SAFE:
            # _pid_is_dead breaks the lock only when the holder provably no longer exists (POSIX
            # os.kill(pid,0); Windows OpenProcess). It never uses os.kill on Windows, which would
            # TERMINATE the holder instead of probing it and silently lose its in-flight write.
            if holder_pid and holder_pid != os.getpid():
                if _pid_is_dead(holder_pid):
                    logger.warning(
                        f"Breaking orphaned lock on {self.path} "
                        f"(holder pid={holder_pid} no longer exists)"
                    )
                    self._force_break()
                    return True
                # else: holder is alive - fall through to the age check

            # Fall back to age check using stored unix timestamp
            acquired_at_ts = meta.get("acquired_at_ts")
            if acquired_at_ts is None:
                # Old-format lock: try to parse from acquired_at string
                acquired_at_raw = meta.get("acquired_at", 0)
                if isinstance(acquired_at_raw, (int, float)):
                    acquired_at_ts = acquired_at_raw
                else:
                    try:
                        from datetime import datetime, timezone
                        acquired_at_ts = datetime.fromisoformat(
                            acquired_at_raw
                        ).timestamp()
                    except (ValueError, TypeError):
                        acquired_at_ts = 0

            lock_age = time.time() - acquired_at_ts
            if lock_age > threshold:
                logger.warning(
                    f"Breaking stale lock on {self.path} "
                    f"(held by pid={holder_pid} for {lock_age:.0f}s)"
                )
                self._force_break()
                return True

            return False
        except (OSError, json.JSONDecodeError):
            return False
    
    def _force_break(self):
        """Force-break a lock by removing the lock directory."""
        try:
            if os.path.exists(self.meta_path):
                os.unlink(self.meta_path)
            os.rmdir(self.lock_dir)
        except OSError:
            pass
    
    def __enter__(self):
        self.acquire()
        return self
    
    def __exit__(self, *exc):
        self.release()
        return False
    
    def __del__(self):
        """Safety net - release lock if holder is garbage collected."""
        if self._held:
            self.release()
