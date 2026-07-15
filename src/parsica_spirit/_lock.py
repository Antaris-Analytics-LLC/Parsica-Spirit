"""parsica_spirit._lock - a small cross-process advisory lock (msvcrt/fcntl).

Lives in its OWN module because the ranking-path modules (router.py, promote.py) are
scanned for wall-clock calls, and a lock timeout is I/O bookkeeping, not a ranking
input. The OS releases the lock when the process dies (kernel-owned, never stale).
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path


class LockHeld(RuntimeError):
    """Another process holds the lock; the caller decides whether that is fatal."""


@contextlib.contextmanager
def exclusive_lock(path, *, timeout: float = 30.0, what: str = "operation"):
    """Hold an exclusive OS advisory lock on `path` for the with-block. Raises
    LockHeld on timeout - loud by design: for last-write-wins stores a silent
    concurrent writer IS the failure, so waiting forever or proceeding anyway are
    both worse than refusing."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(p), os.O_CREAT | os.O_RDWR)
    got = False
    try:
        deadline = time.time() + max(0.0, timeout)
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if time.time() >= deadline:
                    raise LockHeld("another process holds the %s lock (%s)" % (what, p))
                time.sleep(0.05)
        yield
    finally:
        if got:
            try:
                if os.name == "nt":
                    import msvcrt
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        try:
            os.close(fd)
        except OSError:
            pass
