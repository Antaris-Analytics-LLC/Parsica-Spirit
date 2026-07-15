"""parsica_spirit.identity - one workspace-bound, singular, NON-EXPORTABLE identity.

The base tier enforces the two identity invariants mechanically, plus copy/corrupt-fingerprint
QUARANTINE (a copied / moved / unreadable workspace fails CLOSED and is loudly flagged, but is never
locked out - a working agent matters more to an everyday user than a matching identity) and a
deterministic SessionStart identity banner. Identity here is FLEXIBLE, not cryptographic -
stronger identity schemes are explicitly out of scope.

  * SCOPED-ONCE - a workspace can hold exactly one identity. A second `scope_identity` on a
    workspace that already has one is REFUSED (IdentityError). The identity_id is minted exactly
    once and is frozen - rename changes only the display name, never the id.
  * NON-EXPORTABLE - there is no serialization path that emits a portable/duplicable identity
    bundle. The identity is a workspace-local file; `workspace_fingerprint` binds it to THIS
    workspace's absolute path, so a copied folder yields a mismatching fingerprint (the hook the
    quarantine uses). Enforced here by VETO-BY-ABSENCE: no export/bundle/portable/import method
    exists (asserted in the tests).
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from .scrub import scrub

_IDENTITY_FILE = "identity.json"
_SCHEMA_VERSION = 1


class IdentityError(Exception):
    """Raised when the scoped-once invariant is violated, a rename targets a missing identity, or an
    identity mutation (quarantine / rename) can't complete right now under workspace contention (or a
    disk fault) and is conservatively refused with the workspace left exactly as it was."""


def _path(root: str) -> str:
    return os.path.join(root, _IDENTITY_FILE)


def _replace_with_retry(src: str, dst: str, *, attempts: int = 40, delay: float = 0.005,
                        max_delay: float = 0.05) -> None:
    """os.replace, resilient to the Windows sharing violation. A concurrent reader may briefly hold an
    open handle on EITHER path - on `dst` while a load_identity re-inject validates the freshly-published
    file, OR on `src` when we are moving the live identity.json aside for quarantine - and on Windows
    either turns the replace into PermissionError (WinError 5/32). Python's open() grants no share-delete,
    so even a microsecond-long reader blocks the rename. Those handles are short-lived, so a bounded retry
    with mild backoff reliably wins under ordinary polling; a genuine, persistent permission error still
    re-raises for the caller to handle (quarantine converts it into a conservative IdentityError). On
    POSIX this never triggers (rename over an open file is fine) - the first attempt succeeds."""
    d = delay
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(d)
            d = min(d * 1.5, max_delay)


def workspace_fingerprint(root: str) -> str:
    """The stable per-workspace signal the copy-quarantine recomputes and compares (public so the
    quarantine layer reuses this exact logic, never a duplicate). realpath resolves symlinks/junctions and normcase
    case-folds on case-insensitive filesystems, so equivalent spellings of the SAME directory hash
    identically while a genuinely different (e.g. copied) location differs. Full sha256 - no
    truncation; the format is persisted, so it is fixed here once and for all."""
    canonical = os.path.normcase(os.path.realpath(root))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Identity:
    """A workspace identity. `identity_id` is frozen (minted once); `display_name` is mutable
    via rename_identity (which re-mints NOTHING). No method emits a portable bundle."""

    identity_id: str
    display_name: str
    workspace_fingerprint: str
    provenance: str
    created: str
    schema_version: int = _SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "identity_id": self.identity_id,
            "display_name": self.display_name,
            "workspace_fingerprint": self.workspace_fingerprint,
            "provenance": self.provenance,
            "created": self.created,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Identity":
        return cls(
            identity_id=d["identity_id"],
            display_name=d.get("display_name", ""),
            workspace_fingerprint=d.get("workspace_fingerprint", ""),
            provenance=d.get("provenance", "self"),
            created=d.get("created", ""),
            schema_version=int(d.get("schema_version", _SCHEMA_VERSION)),
        )


def _write(root: str, ident: Identity) -> None:
    """Atomic OVERWRITE - used by rename, which only ever mutates display_name, never the id.
    Unique temp (mkstemp) so concurrent writers never collide on the temp path."""
    target = _path(root)
    fd, tmp = tempfile.mkstemp(dir=root, prefix=".identity-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(ident.to_dict(), f, ensure_ascii=False, indent=2)
        _replace_with_retry(tmp, target)   # atomic overwrite of an existing identity (display-name change)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def identity_exists(root: str) -> bool:
    """True if an identity FILE is present - which may be a crash leftover. For a 'is there a valid
    identity?' / 'can I scope?' check, use `load_identity(root) is not None` (what scope_identity
    itself uses): scope_identity repairs an empty/crash-leftover file rather than refusing on it."""
    return os.path.exists(_path(root))


_SECOND_IDENTITY = "this workspace already has an identity (scoped-once); refusing a second"


# Advisory file lock (fcntl on POSIX, msvcrt on Windows) used ONLY to serialize the rare crash-
# recovery repair path. It is released automatically when the fd is closed or the process dies, so
# it can never leave a stale lock that wedges the workspace. On an exotic platform with neither API
# it degrades to a no-op (repair is then best-effort, as it was before the lock).
try:
    if sys.platform == "win32":
        import msvcrt

        def _lock_exclusive(fd: int) -> None:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)

        def _unlock(fd: int) -> None:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        def _lock_exclusive(fd: int) -> None:
            fcntl.flock(fd, fcntl.LOCK_EX)

        def _unlock(fd: int) -> None:
            fcntl.flock(fd, fcntl.LOCK_UN)
except ImportError:   # neither lock API - repair falls back to best-effort (unlocked)
    def _lock_exclusive(fd: int) -> None:
        pass

    def _unlock(fd: int) -> None:
        pass


# The file lock above serializes repair ACROSS PROCESSES; this process-global lock serializes it
# across THREADS in one process (OS byte-range locks are per-handle and not reliably thread-mutually-
# exclusive within a process). Belt-and-suspenders: both levels, so exactly one repairer ever acts.
_REPAIR_LOCK = threading.Lock()


def scope_identity(root: str, *, display_name: str, provenance: str = "self",
                   scrub_pii: bool = True) -> Identity:
    """Mint this workspace's identity ONCE, ATOMICALLY. A second scope - even a concurrent one, on
    ANY filesystem - is refused (IdentityError). The record is written to a UNIQUE temp (mkstemp)
    first, then atomically hard-linked into place: the identity file only ever appears fully-formed
    (crash-safe) and the link fails if it already exists (race-free, no check-then-write TOCTOU).

    Hardlink-less filesystems (FAT/exFAT, some network/FUSE mounts) AND the crash-recovery repair
    share ONE serialized, atomic mint-or-repair path guarded by an advisory lock (a process-global
    thread lock PLUS an OS file lock on <target>.lock): exactly one actor mints or repairs, everyone
    else observes the now-valid identity and refuses. So scoped-once holds and the returned id always
    equals what is persisted - no 0-byte window a racer could misread as a crash leftover and clobber.
    CRASH-RECOVERY: only an exactly-empty identity.json (the crash signature) is repaired with a fresh
    mint; a VALID identity is never overwritten, and a non-empty-but-unreadable file is left intact
    for the fingerprint quarantine."""
    os.makedirs(root, exist_ok=True)
    # the scrub gate covers identity too: a secret/PII in a display name or provenance never lands raw
    display_name, _ = scrub(display_name or "", scrub_pii=scrub_pii)
    provenance, _ = scrub(provenance or "self", scrub_pii=scrub_pii)
    ident = Identity(
        identity_id=secrets.token_hex(16),
        display_name=display_name,
        workspace_fingerprint=workspace_fingerprint(root),
        provenance=provenance,
        created=datetime.now(timezone.utc).isoformat(),
    )
    content = json.dumps(ident.to_dict(), ensure_ascii=False, indent=2)
    target = _path(root)
    # --- fast path: atomic hardlink mint (the normal uncontended case on a hardlink FS; no lock) ---
    fd, tmp = tempfile.mkstemp(dir=root, prefix=".identity-", suffix=".tmp")   # unique per call
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        try:
            os.link(tmp, target)               # primary: atomic + exclusive + crash-safe
            return ident
        except FileExistsError:
            pass                               # target exists - locked mint-or-repair below
        except OSError:
            pass                               # hardlink-less FS - locked mint-or-repair below
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    # --- locked mint-or-repair: reached when the hardlink couldn't complete the mint (target already
    # present, OR a hardlink-less filesystem). ONE serialized actor handles BOTH cases atomically, so
    # there is no unlocked, non-atomic O_EXCL fallback leaving a 0-byte window a concurrent repairer
    # could misread as a crash leftover and clobber (which split the identity). A process-global thread
    # lock covers THIS process; the advisory OS lock on <target>.lock covers OTHER processes. Exactly
    # one actor mints/repairs; everyone else observes the now-valid identity and refuses (scoped-once).
    # load_identity runs ONLY under the lock, so no unlocked reader holds `target` open across the
    # atomic publish.
    with _REPAIR_LOCK:
        lfd = os.open(target + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            _lock_exclusive(lfd)
            if load_identity(root) is not None:
                raise IdentityError(_SECOND_IDENTITY)   # a valid identity exists (or a racer just minted)
            if os.path.exists(target):
                # present but NOT a valid identity: repair only the exactly-empty crash signature;
                # a non-empty-but-unreadable file is left intact for the fingerprint quarantine.
                try:
                    is_empty = os.path.getsize(target) == 0
                except OSError:
                    is_empty = False
                if not is_empty:
                    raise IdentityError(
                        "an unreadable identity file is present; leaving it for quarantine (not overwriting)")
            # mint (hardlink-less, target absent) or repair (empty leftover): publish atomically.
            rfd, rtmp = tempfile.mkstemp(dir=root, prefix=".identity-", suffix=".tmp")
            try:
                with os.fdopen(rfd, "w", encoding="utf-8") as f:
                    f.write(content)
                # we hold both locks (no concurrent minter/repairer); the retry only covers a concurrent
                # cross-caller READER (e.g. a load_identity re-inject) holding dst open on Windows.
                _replace_with_retry(rtmp, target)
            except BaseException:
                try:
                    os.remove(rtmp)
                except OSError:
                    pass
                raise
            return ident
        finally:
            try:
                _unlock(lfd)      # may raise on Windows if the lock was never acquired - don't let
            except OSError:       # that mask the real error or skip the close below
                pass
            finally:
                os.close(lfd)     # ALWAYS close the lock fd (also releases the OS lock). The
                # <target>.lock file is left in place ON PURPOSE: it is one stable, reusable per-
                # workspace advisory-lock file (not an accumulating temp), and unlinking a lock file
                # is racy - a concurrent opener could hold the old inode while a fresh create makes a
                # second, letting two actors believe they hold the lock. Leaving it is the safe choice.


def load_identity(root: str) -> Optional[Identity]:
    """RAW loader: parse identity.json into an Identity, or None if unset/unreadable. Does NOT verify
    the workspace binding - a copied folder's identity loads fine here. For the AUTHORITATIVE, workspace-
    verified identity use `load_identity_status(root)` / `Context.identity` (both fail closed on a copy)."""
    try:
        with open(_path(root), encoding="utf-8") as f:
            return Identity.from_dict(json.load(f))
    except (OSError, ValueError, KeyError):
        return None


# --- Copy/corrupt quarantine + deterministic re-injection ----------------------------------------
# This identity is FLEXIBLE, not cryptographic (stronger identity schemes are out of scope).
# workspace_fingerprint is a copied-folder SPEED-BUMP: it detects that the
# resolved path changed, so a lazily-duplicated workspace does not silently act as the original. It is
# deliberately NOT anti-theft against a determined actor who reconstructs the original absolute path.
# Design stance: on any non-OK state we FAIL OPEN for usability (store / query /
# continuity keep working) but LOUD for trust (the identity reads as absent + the SessionStart banner
# shouts) - a working agent matters more to an everyday user than a matching identity, and we NEVER lock
# them out. Classification is pure/side-effect-free (no write, no lock, no move) so it is run-twice-
# identical and safe on a read-only or shared mount; the ONLY mutation is an explicit re-scope/quarantine.

class IdentityStatus:
    """How the on-disk identity relates to THIS workspace. Plain str constants (JSON/log friendly)."""
    ABSENT = "absent"       # no identity.json
    OK = "ok"               # loads AND its fingerprint matches this workspace
    LEGACY = "legacy"       # loads + valid but has no stored fingerprint (pre-fingerprint) - usable, NOT a copy
    MISMATCH = "mismatch"   # loads but the fingerprint differs - copied / moved workspace
    CORRUPT = "corrupt"     # present, non-empty, unreadable
    EMPTY = "empty"         # zero-byte crash leftover (scope_identity repairs this)


@dataclass(frozen=True)
class IdentityView:
    """Read-only classification of the workspace identity: status, the Identity when usable, and both
    fingerprints for the human/audit path. Pure value object - building it never mutates disk state."""

    status: str
    identity: Optional[Identity]
    recomputed_fingerprint: str
    stored_fingerprint: str

    @property
    def usable(self) -> bool:
        """OK and LEGACY are authoritative for THIS workspace; MISMATCH/CORRUPT/ABSENT/EMPTY are not."""
        return self.status in (IdentityStatus.OK, IdentityStatus.LEGACY)


def load_identity_status(root: str) -> IdentityView:
    """Classify the workspace identity WITHOUT touching disk (no write, no lock, no move) - byte-
    deterministic, run-twice-identical, safe on a read-only mount. This is the check copy/corrupt-
    quarantine and the SessionStart banner hang off of."""
    recomputed = workspace_fingerprint(root)
    target = _path(root)
    if not os.path.exists(target):
        return IdentityView(IdentityStatus.ABSENT, None, recomputed, "")
    try:
        empty = os.path.getsize(target) == 0
    except OSError:
        empty = False
    if empty:
        return IdentityView(IdentityStatus.EMPTY, None, recomputed, "")
    ident = load_identity(root)                       # raw (unverified) load
    if ident is None:
        return IdentityView(IdentityStatus.CORRUPT, None, recomputed, "")
    stored = ident.workspace_fingerprint
    if not stored:                                    # pre-fingerprint schema - valid but old, NOT a copy
        return IdentityView(IdentityStatus.LEGACY, ident, recomputed, "")
    if stored == recomputed:
        return IdentityView(IdentityStatus.OK, ident, recomputed, stored)
    return IdentityView(IdentityStatus.MISMATCH, ident, recomputed, stored)


_BANNER_SENTINEL = "[parsica-spirit] identity:"     # stable prefix so a host hook can detect-and-replace


def render_identity_banner(root: str, *, scrub_pii: bool = True) -> str:
    """The deterministic SessionStart identity line - a PURE function of on-disk state (no clock, no
    randomness), so 1 call or 100 yields byte-identical text and it survives compaction. LOUD on a non-OK
    state (a copied/moved/unreadable workspace SHOUTS that it has no identity) but blocks nothing. Only an
    id-PREFIX is shown (the line lands in transcripts/logs; the full id is on the audit path). '' on an
    absent/unset workspace so a fresh dir isn't nagged every boot."""
    view = load_identity_status(root)
    if view.status in (IdentityStatus.ABSENT, IdentityStatus.EMPTY):
        return ""
    if view.usable and view.identity is not None:
        name = view.identity.display_name or "(unnamed)"
        legacy = "" if view.status == IdentityStatus.OK else "  (legacy - re-scope to bind it to this workspace)"
        line = "%s %s (%s...) - bound to this workspace.%s" % (
            _BANNER_SENTINEL, name, view.identity.identity_id[:12], legacy)
    elif view.status == IdentityStatus.MISMATCH:
        line = ("%s NONE - this workspace does not match its identity (copied or moved). Acting WITHOUT an "
                "identity; provenance shows none until you re-scope locally." % _BANNER_SENTINEL)
    else:  # CORRUPT
        line = ("%s NONE - the identity file is unreadable. Acting WITHOUT an identity; quarantine it and "
                "re-scope to restore a trusted identity." % _BANNER_SENTINEL)
    clean, _ = scrub(line, scrub_pii=scrub_pii)      # defense-scrub: a legacy pre-scrub display_name can't leak
    return clean


# The byline lands in a directive the agent repeats verbatim every response, so BOTH the tag and the
# NAME are neutralized before they touch it - a hostile/garbled/legacy value must never break the
# single-line quoted contract or smuggle a second (e.g. "[system] ...") directive in. scrub() runs
# after for secrets/PII, but injection safety is structural and cannot rest on scrub.
_BYLINE_SENTINEL = "[byline]"     # stable prefix so a host hook can detect the directive
_BYLINE_TAG_MAX = 12
_BYLINE_NAME_MAX = 48             # also caps how much of the injected budget a name can claim


def _safe_byline_tag(session_tag: str) -> str:
    tag = "".join(ch for ch in (session_tag or "") if ch.isalnum() or ch in "-_")
    return tag[:_BYLINE_TAG_MAX]


def _safe_byline_name(name: str) -> str:
    """Neutralize a display name for interpolation into the byline directive. Names are free-form
    (unicode letters, spaces, apostrophes all fine), so this is a targeted denylist, not an allowlist:
    collapse every whitespace run (incl. newlines) to one space, drop control/format chars and the
    directive-structural chars (`"`, `[`, `]`, `·`, backtick), and cap the length - so a name can
    never break the single-line quoted contract, forge a second directive, or crowd the inject."""
    kept = []
    for ch in name or "":
        if ch in '"[]·`':
            continue
        if unicodedata.category(ch) in ("Cc", "Cf"):
            kept.append(" ")                          # control/format -> space, collapsed below
            continue
        kept.append(ch)
    return " ".join("".join(kept).split())[:_BYLINE_NAME_MAX]


def render_byline_directive(root: str, *, session_tag: str = "", scrub_pii: bool = True) -> str:
    """The byline: a one-line signature directive the host injects so the agent opens every response
    with the name this workspace gave it - `[Name]`, or `[Name · <session-tag>]` when the host passes
    the live session tag. The name a user chose showing up on every reply is what makes choosing it
    carry weight, for the user and the agent both.

    Same purity contract as render_identity_banner: a pure function of on-disk state plus the
    caller-supplied tag (no clock, no randomness - the host owns the live session id; this module
    never invents one), so it survives compaction and re-injection byte-identically. It only speaks
    for a USABLE identity: an absent/unset workspace is never nagged, and a copied, moved, or corrupt
    workspace gets '' - signing is a privilege of an identity actually bound to this workspace (the
    banner stays the loud channel for those states). Scrub-gated like the banner."""
    view = load_identity_status(root)
    if not view.usable or view.identity is None:
        return ""
    name = _safe_byline_name(view.identity.display_name or "")
    if not name:
        return ""                                     # unnamed (or all-neutralized) -> nothing to sign
    tag = _safe_byline_tag(session_tag)
    signature = "[%s · %s]" % (name, tag) if tag else "[%s]" % name
    line = ('%s Open every response with the single line "%s" - the name this agent carries. '
            'Once per response, first line, then answer as normal.' % (_BYLINE_SENTINEL, signature))
    clean, _ = scrub(line, scrub_pii=scrub_pii)       # defense-scrub, matching the banner
    return clean


_QUARANTINE_DIR = ".identity_quarantine"


def quarantine_identity(root: str) -> Optional[str]:
    """Move a MISMATCH/CORRUPT identity.json ASIDE - NEVER delete (provenance is kept) - into
    <root>/.identity_quarantine/, leaving the workspace identity-ABSENT so an ordinary scope_identity can
    mint fresh. Idempotent: a no-op returning None when the identity is OK/LEGACY/ABSENT/EMPTY. Runs under
    the SAME advisory lock scope_identity uses, so it can't race a concurrent mint. Returns the sidecar path."""
    with _REPAIR_LOCK:
        lfd = os.open(_path(root) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            _lock_exclusive(lfd)
            view = load_identity_status(root)
            if view.status not in (IdentityStatus.MISMATCH, IdentityStatus.CORRUPT):
                return None
            # The WHOLE move-aside (mkdir + unique sidecar + the contended replace) is all-or-nothing from
            # the caller's view. Its SOURCE is the live identity.json, which the lock-free readers
            # (load_identity_status / banner / Context.identity) hold open on Windows, and a genuine disk
            # fault (ENOSPC/permission) can hit the setup calls too. A generous backed-off retry lets
            # ordinary reader polling clear; on ANY OSError - a starved retry OR a setup fault - fail
            # CONSERVATIVELY: remove any half-made sidecar and raise the documented IdentityError, leaving
            # identity.json exactly in place (unchanged, still flagged). The recovery path must NEVER crash
            # its caller with a raw OSError (usable-not-locked): a clean, retryable signal, never a stack
            # trace out of the very op meant to restore a trusted identity.
            sidecar = None
            try:
                qdir = os.path.join(root, _QUARANTINE_DIR)
                os.makedirs(qdir, exist_ok=True)
                reason = "mismatch" if view.status == IdentityStatus.MISMATCH else "corrupt"
                fd, sidecar = tempfile.mkstemp(dir=qdir, prefix="identity.%s." % reason, suffix=".json")
                os.close(fd)                                 # unique, non-clobbering sidecar name
                _replace_with_retry(_path(root), sidecar, attempts=80, delay=0.005, max_delay=0.025)
            except OSError as e:
                if sidecar is not None:
                    try:
                        os.remove(sidecar)
                    except OSError:
                        pass
                raise IdentityError(
                    "could not quarantine the identity right now (workspace busy: %s); identity left in "
                    "place, nothing changed - retry" % e.__class__.__name__)
            return sidecar
        finally:
            try:
                _unlock(lfd)
            except OSError:
                pass
            finally:
                os.close(lfd)


def rescope_after_move(root: str, *, display_name: str, provenance: str = "self",
                       scrub_pii: bool = True) -> Identity:
    """Resolve a copied/moved (MISMATCH) or unreadable (CORRUPT) workspace LOCALLY: quarantine the old
    identity aside (kept, never deleted) and mint a BRAND-NEW identity_id bound to THIS workspace. The old
    id NEVER carries across the fingerprint boundary - the non-exportable veto made operational: a
    duplicated folder can't BECOME the original, it can only start its own local identity. Refuses when the
    workspace already has a usable (OK/LEGACY) identity; an ABSENT/EMPTY workspace just mints."""
    view = load_identity_status(root)
    if view.usable:
        raise IdentityError("this workspace already has a usable identity; nothing to re-scope")
    quarantine_identity(root)                            # no-op on ABSENT/EMPTY
    return scope_identity(root, display_name=display_name, provenance=provenance, scrub_pii=scrub_pii)


def rename_identity(root: str, *, display_name: str, scrub_pii: bool = True) -> Identity:
    """Change only the mutable display name (scrub-gated). The identity_id is NEVER re-minted. Refuses on
    a non-usable workspace (absent / copied-or-moved / corrupt) - you can't rename an identity that isn't
    validly bound to this workspace; re-scope first.

    Rename is a MUTATOR, so it runs under the SAME advisory lock as scope / quarantine / rescope (a
    process-global thread lock PLUS the OS file lock on <target>.lock) and RE-READS the status UNDER the
    lock, immediately before the write. That closes the check-then-write window in which a concurrent
    quarantine could move the identity aside between an unlocked check and the write, and rename would
    otherwise resurrect the just-excised id - the sole unlocked mutator was the gap."""
    display_name, _ = scrub(display_name or "", scrub_pii=scrub_pii)
    with _REPAIR_LOCK:
        lfd = os.open(_path(root) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            _lock_exclusive(lfd)
            view = load_identity_status(root)             # authoritative read UNDER the lock
            if not view.usable or view.identity is None:
                raise IdentityError(
                    "no usable identity to rename (workspace is absent, copied/moved, or corrupt)")
            ident = view.identity
            renamed = Identity(
                identity_id=ident.identity_id,   # preserved
                display_name=display_name,
                workspace_fingerprint=ident.workspace_fingerprint,
                provenance=ident.provenance,
                created=ident.created,
            )
            try:
                _write(root, renamed)                     # overwrite while no quarantine can interleave
            except OSError as e:
                # identity.json is the REPLACE target here, so the same lock-free-reader contention that
                # can starve quarantine's move can starve this write on Windows. Never leak a raw OSError
                # from a public identity op: rename is display-name-only and non-destructive, so a starved
                # write is a clean, retryable no-change signal, not a crash (usable-not-locked).
                raise IdentityError(
                    "could not rename the identity right now (workspace busy: %s); name unchanged - retry"
                    % e.__class__.__name__)
            return renamed
        finally:
            try:
                _unlock(lfd)
            except OSError:
                pass
            finally:
                os.close(lfd)
