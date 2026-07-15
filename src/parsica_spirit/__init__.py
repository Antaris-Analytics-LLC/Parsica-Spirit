"""parsica_spirit - Parsica Spirit, persistent memory for Claude Code. One package:
this facade (the base tier's surface), the contracts leaf (the `enrich` subpackage), and
the memory engine (the `engine` subpackage) ship together in a single wheel.

WIRING (dependency inversion): the storage contracts (StoragePort / AdminBackend) live in
the shared leaf, so both this facade and the engine implement them by importing only the
leaf - there is no edge between them. The engine backend is INJECTED into the facade by
the application; the facade never imports or dynamically loads the engine subpackage
(CI-enforced). The base tier runs on markdown/JSONL alone.
"""

from parsica_spirit.enrich import (
    DISPOSITIONS,
    AdminBackend,
    PlanError,
    ResetPlan,
    StoragePort,
    StoredRecord,
)

from .context import Context, WriteResult, open_markdown
from .continuity import Continuity
from .identity import (
    Identity,
    IdentityError,
    IdentityStatus,
    IdentityView,
    identity_exists,
    load_identity,
    load_identity_status,
    quarantine_identity,
    render_byline_directive,
    render_identity_banner,
    rename_identity,
    rescope_after_move,
    scope_identity,
    workspace_fingerprint,
)
from .scrub import looks_clean, scrub
from .claude_memory import (
    ClaudeRecord, ImportReport, import_claude_memory, import_records, discover_records,
)

__all__ = [
    # seam contracts (re-exported from the leaf)
    "StoragePort", "AdminBackend", "StoredRecord", "ResetPlan", "PlanError", "DISPOSITIONS",
    # product facade (base tier)
    "Context", "WriteResult", "open_markdown",
    # continuity surface (working set / journal / lessons; survives compaction)
    "Continuity",
    # workspace identity (scoped-once, non-exportable) + copy/corrupt quarantine + re-injection
    "Identity", "IdentityError", "IdentityStatus", "IdentityView", "scope_identity", "load_identity",
    "load_identity_status", "rename_identity", "identity_exists", "workspace_fingerprint",
    "quarantine_identity", "rescope_after_move", "render_identity_banner", "render_byline_directive",
    # mandatory scrub gate
    "scrub", "looks_clean",
    # Claude Code memory import (CLAUDE.md / MEMORY.md -> Parsica; import-only, idempotent)
    "ClaudeRecord", "ImportReport", "import_claude_memory", "import_records", "discover_records",
]
__version__ = "1.0.0"
