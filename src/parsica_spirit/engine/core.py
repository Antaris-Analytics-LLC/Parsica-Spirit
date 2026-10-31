"""
MemorySystem - Production-ready memory system with sharding, BM25 search, and concurrency safety.

Features:
- Sharded storage for scalability (10K+ memories)
- BM25-inspired search with IDF weighting and field boosting
- File locking and optimistic conflict detection
- Fast indexes (full-text, tags, dates)
- Schema migration with backward compatibility

Usage:
    from parsica_spirit.engine import MemorySystem

    mem = MemorySystem("./workspace")
    mem.load()
    mem.ingest("Key decision", source="meeting", category="strategic")
    results = mem.search("decision")
    mem.save()  # Saves to sharded format
"""

import json
import logging
import os
import re
import time
from collections import defaultdict

logger = logging.getLogger("parsica_spirit.engine")

# Pre-ingest metadata junk filter
# Rejects lines that look like raw JSON key-value pairs or bare structural
# JSON tokens. These are never meaningful memories.
#   Matches:  "message_id": "1234"
#             { / } / [ / ]  standing alone
_METADATA_LINE_RE = re.compile(
    r'^[{\[\]},]$'                                     # bare JSON structural tokens
    r'|^\s*"[a-z_]{2,40}"\s*:\s*'                     # JSON key-value lines "key": ...
    r'|^<!--.*-->$'                                     # HTML comment markers
    r'|^\s*```[a-z]*\s*$'                              # fenced code block delimiters
    r'|^\s*-{3,}\s*$'                                  # horizontal rule separators
    r'|^\[System Message\]'                             # system message lines
    r'|^Reasoning:\s*$',                               # bare reasoning labels
    re.IGNORECASE
)

# Code line detection for ingest quality filtering
_CODE_LINE_RE = re.compile(
    r'^\s{4,}\S'                    # 4+ space indented code
    r'|^export\s+\w+='             # env var exports
    r'|^[A-Z_]{3,}=\S'            # ALL_CAPS=value assignments
    r'|^\$\s'                       # shell prompt lines
    r'|^>>>\s'                      # Python REPL lines
    r'|^\s*import\s+\w'            # import statements
    r'|^\s*from\s+\w+\s+import'   # from X import Y
    r'|^\s*def\s+\w+\('           # function definitions
    r'|^\s*class\s+\w+[:\(]'      # class definitions
)

# Credential detection for security filtering
from parsica_spirit.enrich.scrub import scrub   # shared leaf scrubber; the Context facade uses the same one

_CREDENTIAL_RE = re.compile(
    r'sk-ant-api\d{2}-\w{20,}'      # Anthropic API keys
    r'|sk-proj-\w{20,}'             # OpenAI API keys  
    r'|hf_\w{20,}'                  # HuggingFace tokens
    r'|eyJ[A-Za-z0-9_-]{50,}'       # JWT tokens (base64-encoded JSON)
    r'|ghp_\w{20,}'                 # GitHub personal tokens
    r'|ghu_\w{20,}'                 # GitHub user tokens
    r'|MTQ\w{20,}\.\w{5,}\.\w{20,}' # Discord bot tokens
    r'|Bearer\s+\w{20,}'            # Bearer tokens
    r'|token[=:]\s*\w{30,}'         # Generic token assignments
    r'|password[=:]\s*\S{8,}'       # Password assignments
    r'|PRIVATE.KEY'                  # Private key markers
)

# Conversation noise filter for agent memory quality.
# Matches short acknowledgments, status messages, and flow-control phrases
# that are conversational but carry no knowledge worth remembering.
# Applied only to pipeline/agent_turn sources (not user-typed memories).
_CONVERSATION_NOISE_RE = re.compile(
    r'^(?:OUTPUT:\s*)?(?:'
    r'(?:Understood|Got it|On it|Sure|OK|Ready|Done|Noted|Will do|Perfect|'
    r'Sounds good|Absolutely|Alright|Acknowledged|Affirmative|Roger|Copy that|'
    r'No problem|Of course|Certainly|Definitely|Right away|'
    r'Let me (?:check|look|see|think|work)|'
    r'I\'ll (?:check|look|handle|work|take|get)|'
    r'Looking (?:into|at)|Checking (?:now|that)|Starting (?:now|on)|'
    r'Working on (?:it|that|this)|Running (?:now|that))\b'
    r'|^(?:Turn:\s*)?<@\d+>\s*.{0,15}$'     # bare mentions with <=15 chars after
    r'|^\[\[reply_to_current\]\]'            # reply markers
    r')',
    re.IGNORECASE
)

# Turn/INPUT/OUTPUT prefix stripping for content normalization.
# When checking for duplicates, the same message stored as "Turn: X",
# "INPUT: X", and "OUTPUT: X" should be treated as one memory, not three.
_TURN_PREFIX_RE = re.compile(r'^(?:Turn|INPUT|OUTPUT|System):\s*')


def _iso_ts_or_none(value):
    """The original string when it is a valid ISO-8601 datetime, else None. An imported timestamp
    is UNTRUSTED external text (GPT review S2): a credential/PII-shaped string in the field would
    otherwise ride raw into created/last_accessed, the index files, and the shard filename. Validate
    without transforming - a valid stamp is kept VERBATIM so export->import round-trips are byte-stable
    (export writes a naive isoformat); a trailing 'Z' is probed as +00:00 so UTC stamps parse on 3.9."""
    raw = str(value or "").strip()
    if not raw:
        return None
    probe = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        datetime.fromisoformat(probe)   # `datetime` is module-global by call time (imported below)
    except ValueError:
        return None
    return raw

import copy
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .entry import MemoryEntry
from .decay import DecayEngine
from .sentiment import SentimentTagger
from .temporal import TemporalEngine
from .confidence import ConfidenceEngine
from .compression import CompressionEngine
from .forgetting import ForgettingEngine
from .consolidation import ConsolidationEngine
from .gating import InputGate
from .synthesis import KnowledgeSynthesizer
from .sharding import ShardManager
from .migration import MigrationManager
# Incremental Index Loading + Time-Window Shard Splitting
from .wal import WALWriter, WALReader
from .shards import ShardManager as TimeShardManager
from .migrate import migrate_to_shards as _migrate_to_shards
from .indexing import IndexManager
from .search import SearchEngine, SearchResult
from .context_packet import ContextPacket, ContextPacketBuilder
from .namespace import NamespaceManager
from .memory_types import (
    MEMORY_TYPE_CONFIGS, DEFAULT_TYPE, get_type_config,
    format_mistake_content, SEVERITY_LEVELS,
    normalize_memory_type, sanitize_type_config,
)
from .utils import atomic_write_json
from .performance import ReadCache, WALManager, PerformanceMonitor, AccessTracker
from .category_tagger import CategoryTagger
from .session_tracker import SessionTracker, sid_tag

# Module-level CategoryTagger singleton - shared across all ingest paths
_ct = CategoryTagger()

# Default tags to auto-extract
_DEFAULT_TAG_TERMS = [
    "web3", "ethereum", "postgresql", "optimization", "cost",
    "revenue", "security", "deployment", "production", "testing",
]


class MemorySystem(NamespaceManager):
    """Production-ready memory system with sharding and indexing.

    Storage mode parameters
    -----------------------
    Three flags control the storage/sharding subsystem. They interact as
    follows (evaluated in priority order during load/save):

    ``use_sharding`` (bool, default True)
        Enables the **sharded** storage system (``sharding.py`` /
        ``ShardManager``).  Date + topic partitioning into ``shards/`` dir.
        When True, this takes priority over ``sharding``.

    ``sharding`` (bool, default True)
        Enables the **time-window** shard system (``shards.py`` /
        ``TimeShardManager``).  Weekly JSONL files
        (``memories_YYYY-WXX.jsonl``).  Only active when
        ``use_sharding=False``.

    ``tiered_storage`` (bool, default True)
        Hot/warm/cold tier classification.  Only active when
        ``use_sharding=False`` AND ``sharding=True``.  Hot tier loaded
        on startup; warm/cold loaded on-demand during search.

    Typical configurations:
        - **Sharded** (default): ``use_sharding=True`` - ignores
          ``sharding`` and ``tiered_storage``.
        - **Time-window + tiers**: ``use_sharding=False, sharding=True,
          tiered_storage=True``.
        - **Legacy flat file**: ``use_sharding=False, sharding=False``.

    Other parameters
    ----------------
    workspace : str
        Root directory. Shards and indexes are stored here.
    half_life : float
        Decay half-life in days (default 7).
    tag_terms : list[str] | None
        Custom terms to auto-tag. Merged with built-in defaults.
    """

    def __init__(
        self,
        workspace: str = ".",
        half_life: float = 7.0,
        tag_terms: List[str] = None,
        use_sharding: bool = True,
        use_indexing: bool = True,
        enable_read_cache: bool = True,
        cache_max_entries: int = 1000,
        agent_name: Optional[str] = None,
        # time-window shard splitting + incremental index loading
        sharding: bool = True,
        # LLM-enriched indexing - any callable(str) -> dict
        enricher: Optional[Callable[[str], dict]] = None,
        # tiered storage - load hot tier only on startup
        tiered_storage: bool = True,
        # graph intelligence - entity extraction + relationship-aware search
        graph_intelligence: bool = True,
        # session isolation - trigger-based multi-session write gate:
        # "off" (default, no gate) | "open" (permissive; idle writes logged)
        # | "closed" (strict: only sessions activated via begin_turn may write)
        session_awareness: str = "off",
    ):
        self.workspace = os.path.abspath(workspace)
        # agent_name is external metadata that lands in provenance/labels on disk - scrub it
        # here so a secret/PII-bearing name never persists (idempotent; None passes through).
        self.agent_name = self._scrub_str(agent_name)
        if agent_name is None:
            import warnings
            warnings.warn(
                "[parsica-spirit engine] agent_name not set. Memories will not be agent-scoped. "
                "Pass agent_name='your-agent-name' to MemorySystem() to enable per-agent "
                "isolation and correct cross-instance context restore.",
                UserWarning,
                stacklevel=2,
            )
        self.use_sharding = use_sharding
        self.use_indexing = use_indexing

        # Backward compatibility paths
        self.legacy_metadata_path = os.path.join(self.workspace, "memory_metadata.json")
        self.audit_path = os.path.join(self.workspace, "memory_audit.json")

        # v0.4 managers
        self.migration_manager = MigrationManager(workspace)
        self.shard_manager = ShardManager(workspace) if use_sharding else None
        self.index_manager = IndexManager(workspace) if use_indexing else None

        # Engines (same as v0.3)
        self.decay = DecayEngine(half_life=half_life)
        self.sentiment = SentimentTagger()
        self.temporal = TemporalEngine()
        self.confidence = ConfidenceEngine()
        self.compression = CompressionEngine()
        self.forgetting = ForgettingEngine()
        self.consolidation = ConsolidationEngine(decay=self.decay)
        self.gating = InputGate()
        self.synthesis = KnowledgeSynthesizer()
        self.search_engine = SearchEngine()

        # Tag terms
        self._tag_terms = list(set(_DEFAULT_TAG_TERMS + (tag_terms or [])))

        # Memory store (in-memory cache)
        self.memories: List[MemoryEntry] = []
        self._hashes: set = set()
        self._content_norms: set = set()  # Content-normalized dedup guard

        # performance subsystem
        self._read_cache: Optional[ReadCache] = (
            ReadCache(cache_max_entries) if enable_read_cache else None
        )
        self._enable_read_cache = enable_read_cache
        self._wal = WALManager(self.workspace)
        self._perf = PerformanceMonitor()
        self._access_tracker = AccessTracker(self.workspace)

        # LLM enricher (any callable: str -> {tags, summary, keywords})
        self._enricher: Optional[Callable[[str], dict]] = enricher
        # Enrichment counter (lifetime, resets on get_enrichment_count(reset=True))
        self._enrichment_count: int = 0

        # Instrumentation + Co-occurrence
        from .instrumentation import SearchContext
        from .cooccurrence import CooccurrenceIndex
        self._cooccurrence = CooccurrenceIndex(self.workspace)
        self._cooccurrence.load()
        self._instrumentation_log_path = os.path.join(self.workspace, ".instrumentation_log.jsonl")

        # -- Time-Window Shard Splitting -----------------------------
        # ``sharding`` controls the NEW weekly shard system (shards.py).
        # It is only active when ``use_sharding=False`` so the sharded
        # storage system (sharding.py) takes priority when enabled.
        self._time_window_sharding: bool = sharding
        self._time_window_shards: Optional[TimeShardManager] = (
            TimeShardManager(self.workspace) if sharding else None
        )

        # -- Tiered Storage -------------------------------------------
        # When tiered_storage=True and the shard system is active, only
        # the hot tier (0-3 days) is loaded into self.memories on startup.
        # Warm-tier shards (3-14 days) are loaded on-demand when a search
        # returns fewer than WARM_FALLBACK_THRESHOLD results.
        self._tiered_storage: bool = tiered_storage and sharding and not use_sharding
        if self._tiered_storage:
            from .tiers import TierManager
            self._tier_manager: Optional["TierManager"] = TierManager(self.workspace)
        else:
            self._tier_manager = None
        # Warm shard names populated during _load_time_window_shards(); used by search()
        self._warm_shard_names: List[str] = []
        self._cold_shard_names: List[str] = []

        # -- Graph Intelligence ---------------------------------------
        # Entity extraction + relationship-aware search boost.
        # Disabled when graph_intelligence=False (zero overhead).
        self._graph_enabled: bool = graph_intelligence
        if self._graph_enabled:
            try:
                from .graph import MemoryGraph
                self._graph: Optional["MemoryGraph"] = MemoryGraph(self.workspace)
            except Exception:
                logger.warning("Graph intelligence init failed; disabling", exc_info=True)
                self._graph_enabled = False
                self._graph = None
        else:
            self._graph = None

        # -- Session isolation: trigger-based multi-session write gate -------
        _VALID_SA_VALUES = ("off", "open", "closed")
        if session_awareness in ("open", "closed"):
            _sa_effective = session_awareness
        elif session_awareness != "off":
            # Invalid value: WARN loudly instead of silently falling back -
            # a typo like "closd" must not leave the user thinking they have
            # session isolation when they don't.
            import warnings
            warnings.warn(
                f"[parsica-spirit engine] Invalid session_awareness value: {session_awareness!r}. "
                f"Valid values are: {_VALID_SA_VALUES}. Falling back to 'off'. "
                f"This means NO session isolation is active - this is likely a bug.",
                UserWarning,
                stacklevel=2,
            )
            logger.warning(
                "Invalid session_awareness=%r - falling back to 'off'. "
                "Valid values: %s. No session isolation is active.",
                session_awareness, _VALID_SA_VALUES,
            )
            _sa_effective = "off"
        else:
            _sa_effective = "off"
        self._session_awareness: str = _sa_effective
        self._session_tracker: Optional[SessionTracker] = (
            SessionTracker(mode=_sa_effective) if _sa_effective != "off" else None
        )

        # -- Incremental Index Loading -------------------------------
        self._index_path: str = os.path.join(self.workspace, ".parsica_index.json")
        self._index_wal_path: str = os.path.join(self.workspace, ".parsica_wal.jsonl")
        self._index_wal_writer: WALWriter = WALWriter()
        self._index_wal_writer.open(self._index_wal_path)
        self._index_wal_entry_count: int = 0  # entries written since last flush

        # Initialize system
        self._initialize()

    def _initialize(self):
        """Initialize the memory system, handling migrations if needed."""
        import logging  # Bug fix: moved to top of method - was inside success branch only
        _log = logging.getLogger("parsica_spirit.engine")
        # Check if migration is needed
        if self.migration_manager.needs_migration():
            migration_result = self.migration_manager.migrate()
            if migration_result["status"] == "success":
                _log.info(
                    f"Migrated from {migration_result['from_version']} to {migration_result['to_version']}")
            elif migration_result["status"] == "error":
                _log.error(
                    f"Migration failed: {migration_result['message']}")
                # Fall back to legacy format
                self.use_sharding = False
                self.use_indexing = False

        # namespace manager
        self._init_namespace_manager()

        # Load existing data
        self.load()

        # Set up incremental index (after memories are loaded)
        self._setup_incremental_index()

    # -- internal helpers ------------------------------------------------

    def _rebuild_content_indexes(self) -> None:
        """Rebuild in-memory dedup indexes from self.memories.

        Consolidates the hash set and content-norm set from the current
        memory list. Called after any bulk mutation
        (load, compact, forget, import) to keep indexes consistent.

        Previously this 5-line pattern was copy-pasted in 6+ places.
        """
        self._hashes = {m.hash for m in self.memories}
        self._content_norms = {
            _TURN_PREFIX_RE.sub('', m.content).strip().lower()
            for m in self.memories
        }

    # -- metadata scrub helpers ------------------------------------------
    # The per-line content scrub in ingest()/_ingest_typed()/ingest_with_gating()
    # only cleans the memory TEXT. Everything else that reaches disk - labels,
    # ids, provenance, channel, tags, type_config, and enricher output - is
    # persisted verbatim via MemoryEntry.to_dict(), the shard filename (derived
    # from tags), the search/tag indexes, and the co-occurrence index. These
    # helpers gate those surfaces through the same leaf scrub, so nothing lands
    # a raw secret or PII on disk. All are idempotent (an already-clean value
    # costs a no-op re-scan) so double-scrubbing across ingest paths is safe.

    def _scrub_str(self, value):
        """Redact secrets (always) and PII from a single value.

        Non-string / empty values pass through untouched. Mirrors the per-line
        content scrub for scalar metadata surfaces (source, ids, provenance,
        channel, urls).
        """
        if isinstance(value, str) and value:
            return scrub(value, scrub_pii=True)[0]
        return value

    def _scrub_tag_list(self, tags):
        """Scrub each string element of a tag/keyword-style list.

        None and non-list/tuple inputs pass through untouched; non-string
        elements are left as-is. Returns a new list when scrubbing applies.
        """
        if not isinstance(tags, (list, tuple)):
            return tags
        return [self._scrub_str(t) if isinstance(t, str) else t for t in tags]

    def _scrub_jsonish(self, value):
        """Recursively scrub strings inside a JSON-ish value.

        Walks str / list / tuple / dict. Dict KEYS are left intact (they are
        field names, not caller data); only values are scrubbed. Used for
        free-form metadata dicts such as type_config.
        """
        if isinstance(value, str):
            return self._scrub_str(value)
        if isinstance(value, list):
            return [self._scrub_jsonish(v) for v in value]
        if isinstance(value, tuple):
            return tuple(self._scrub_jsonish(v) for v in value)
        if isinstance(value, dict):
            return {k: self._scrub_jsonish(v) for k, v in value.items()}
        return value

    def _accept_ingress(self, memory_type, type_config):
        """The ONE ingress gateway for externally supplied memory_type / type_config.

        GPT's common cause: schema-valid does not mean policy-safe - externally supplied
        metadata is only safe to trust once it has been canonicalised. So EVERY ingress
        (ingest, import_from, ...) routes its caller-supplied type through here, one place,
        rather than each re-deriving the rules (enforcers-discover-not-list):
          * memory_type -> a clean slug; a hostile/malformed one becomes DEFAULT_TYPE (the
            memory is still stored, never dropped - only the identifier is neutralised);
          * type_config -> only the allowlisted keys, each numeric a finite float clamped
            into range, the label scrubbed and length-capped; every other key dropped.
        Returns (safe_memory_type, safe_type_config)."""
        safe_type = normalize_memory_type(memory_type)
        # A clean slug can still be a credential SHAPE: a lowercase api key matches the slug regex,
        # so the syntax check alone lets it persist as a type identifier (GPT review S2). If the
        # secret scrub would alter it, it is a secret, not a type - collapse to the default rather
        # than substitute (a redaction marker is not a slug). The memory is still stored.
        if isinstance(safe_type, str) and safe_type != self._scrub_str(safe_type):
            safe_type = DEFAULT_TYPE
        safe_config = sanitize_type_config(type_config, scrub=self._scrub_str)
        return safe_type, safe_config

    @staticmethod
    def _entry_recall_priority(entry) -> float:
        """The recall_priority multiplier for one scored entry.

        Canonical types read their fixed config. A CUSTOM type reads the override that
        rides the entry itself - ingest stores the ingress-sanitised type_config at
        type_metadata["_type_config"] - resolved through get_type_config, which
        re-sanitises on read (an imported entry's metadata is caller data; ranking must
        never see an unclamped number). A custom type with no override gets the episodic
        default, per get_type_config's contract - never a free boost above the canonical
        types just for being unknown."""
        mtype = getattr(entry, "memory_type", DEFAULT_TYPE) or DEFAULT_TYPE
        cfg = MEMORY_TYPE_CONFIGS.get(mtype)
        if cfg is None:
            override = (getattr(entry, "type_metadata", {}) or {}).get("_type_config")
            cfg = get_type_config(mtype, override)
        return cfg.get("recall_priority", 1.0)

    def _scrub_enrichment_dict(self, enrichment):
        """Return an enricher-output dict with its persisted text fields scrubbed.

        Scrubs summary (str) plus the tags / keywords / search_queries / facts
        lists - every enrichment field that reaches disk, the search index, or
        the co-occurrence index. Non-dict input is returned untouched; other
        keys are preserved. Called ONCE right after the enricher runs so no
        copy / co-occurrence-feed / decompose path ever sees the raw output.
        The enricher is an external callable and can echo secrets/PII.
        """
        if not isinstance(enrichment, dict):
            return enrichment
        cleaned = dict(enrichment)
        if "summary" in cleaned:
            cleaned["summary"] = self._scrub_str(cleaned["summary"])
        for _field in ("tags", "keywords", "search_queries", "facts"):
            if _field in cleaned:
                cleaned[_field] = self._scrub_tag_list(cleaned[_field])
        return cleaned

    def _decompose_facts(
        self,
        enrichment: dict,
        parent_entry: "MemoryEntry",
        source: str,
        category: str,
        session_id: str,
        agent_id: Optional[str],
    ) -> int:
        """Extract atomic facts from an enrichment result and store them.

        Applies hash and content-norm dedup checks. Returns the
        count of new fact entries created. Used by ingest(), re_enrich(),
        and re_decompose() to eliminate the 3x duplication of this logic.
        """
        facts = enrichment.get("facts", [])
        if not facts or not isinstance(facts, list):
            return 0

        count = 0
        for fact_text in facts:
            if not isinstance(fact_text, str):
                continue
            # Scrub secrets + PII from the fact BEFORE the length check and
            # before it becomes a stored MemoryEntry. This is the last
            # guaranteed gate for decomposed facts (re_decompose reaches it too).
            fact_text = self._scrub_str(fact_text)
            if len(fact_text.strip()) < 10:
                continue

            fact_entry = MemoryEntry(
                fact_text.strip(),
                source=source,
                line=0,
                category=category,
                memory_type="fact",
                session_id=session_id,
                agent_id=agent_id or self.agent_name,
            )
            fact_entry.provenance = "decomposed"
            fact_entry.parent_hash = parent_entry.hash
            fact_entry.confidence = 0.8
            fact_entry.tags = list(getattr(parent_entry, "tags", []))

            # Dedup: exact hash
            if fact_entry.hash in self._hashes:
                continue
            # Dedup: normalized content
            fact_norm = fact_text.strip().lower()
            if fact_norm in self._content_norms:
                continue

            self.memories.append(fact_entry)
            self._hashes.add(fact_entry.hash)
            self._content_norms.add(fact_norm)
            self.search_engine.mark_dirty()
            if self.use_indexing and self.index_manager:
                self.index_manager.add_memory(fact_entry)
            self._wal.append(fact_entry.to_dict())
            count += 1

        return count

    # -- persistence -----------------------------------------------------

    def save(self) -> str:
        """Save memory state to disk using the appropriate format."""
        # persist graph alongside memories
        if self._graph_enabled and self._graph is not None:
            try:
                self._graph.save()
            except Exception:
                logger.warning("Graph save failed", exc_info=True)
        if self.use_sharding and self.shard_manager:
            return self._save_sharded()
        if not self.use_sharding and self._time_window_sharding and self._time_window_shards:
            return self._save_time_window_sharded()
        return self._save_legacy()
    
    def _save_sharded(self) -> str:
        """Save using v0.4 sharded format."""
        if not self.shard_manager:
            return self._save_legacy()

        # Group memories by shard
        shard_groups = self.shard_manager.shard_memories(self.memories)
        expected_shards = set(shard_groups.keys())

        # Save each expected shard and rebuild the in-memory shard index from truth
        self.shard_manager.index.shards = {}
        for shard_key, shard_memories in shard_groups.items():
            self.shard_manager.save_shard(shard_key, shard_memories)
            self.shard_manager.index.add_shard(shard_key, shard_memories)

        # Remove obsolete shard files so deleted memories cannot resurrect on reload
        self.shard_manager.delete_obsolete_shards(expected_shards)

        # Save shard index
        self.shard_manager.index.save_index()

        # Update search indexes
        if self.use_indexing and self.index_manager:
            self.index_manager.rebuild_indexes(self.memories)
            self.index_manager.save_all_indexes()

        shard_dir = os.path.join(self.workspace, "shards")
        return shard_dir
    
    def _save_time_window_sharded(self) -> str:
        """Save using the weekly time-shard format.

        When tiered storage is active, only hot-tier entries are in
        self.memories.  Warm/cold shard files must be protected from deletion
        by rewrite_entries() - otherwise a save() after ingest would wipe the
        warm/cold tiers that were never loaded into memory.
        """
        if not self._time_window_shards:
            return self._save_legacy()

        # Collect warm/cold shard names to protect from deletion.
        # Also refresh them from disk first in case this instance mutated state
        # after startup (delete_source()/forget()/save()) while another process
        # created or migrated additional warm/cold shards in the same workspace.
        protected: set = set()
        if self._tiered_storage and self._tier_manager is not None:
            all_shards = self._time_window_shards.list_shards()
            _hot, warm, cold = self._tier_manager.classify_shards(all_shards)
            self._warm_shard_names = warm
            self._cold_shard_names = cold
            protected.update(warm)
            protected.update(cold)

        shard_names = self._time_window_shards.rewrite_entries(
            self.memories, protected_shards=protected if protected else None,
        )

        if self._tiered_storage and self._tier_manager is not None:
            hot, warm, cold = self._tier_manager.classify_shards(shard_names)
            self._warm_shard_names = warm
            self._cold_shard_names = cold
            self._tier_manager.invalidate_warm_cache()
            self._tier_manager.save_hashes(shard_names)

        return self.workspace

    def _save_legacy(self) -> str:
        """Save using v0.2/v0.3 legacy single-file format."""
        from . import __version__ as _pkg_version  # Bug fix: distinguish format vs pkg version
        data = {
            "version": "0.4.0",           # storage schema version (used by migration logic)
            "package_version": _pkg_version,  # parsica-spirit package version
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "count": len(self.memories),
            "format": "legacy",
            "memories": [m.to_dict() for m in self.memories],
        }
        atomic_write_json(self.legacy_metadata_path, data)
        return self.legacy_metadata_path

    # -- WAL flush / close -------------------------------------------------

    def flush(self) -> Dict:
        """Compact the WAL into permanent shard/legacy storage.

        Called automatically after ``WALManager.flush_interval`` writes or
        when the WAL exceeds 1 MB.  Also called by ``close()``.

        Returns:
            ``{"flushed_entries": N, "wal_cleared": True}``
        """
        pending_entries = self._wal.load_pending()
        pending_before = len(pending_entries)
        pending_hashes = {entry.get("hash") for entry in pending_entries if entry.get("hash")}

        # Persist current in-memory state first. This makes the snapshot durable
        # before the WAL is rewritten/cleared, eliminating the data-loss window
        # from clear-before-save ordering.
        self.save()

        # Persist access counts alongside the flush
        self._access_tracker.save()

        # Persist co-occurrence index (deferred from ingest for bulk-write perf)
        self._cooccurrence.save()

        # Rewrite WAL to keep only entries that were appended during/after save.
        remaining_entries = []
        for entry in self._wal.load_pending():
            entry_hash = entry.get("hash")
            if entry_hash and entry_hash in pending_hashes:
                continue
            remaining_entries.append(entry)

        if remaining_entries:
            self._wal.replace_with_entries(remaining_entries)
        else:
            self._wal.clear()

        # Invalidate read-cache (data on disk changed)
        if self._read_cache is not None:
            self._read_cache.invalidate()

        # Rebuild the cluster index at flush time (not at search time).
        # Clusters are O(nxe) to build - acceptable at write-flush, not at search.
        self.search_engine.rebuild_clusters(self.memories)

        self._perf.record_compaction()

        return {"flushed_entries": pending_before, "wal_cleared": self._wal.pending_count() == 0}

    def close(self) -> None:
        """Flush WAL and release resources.  Call before process exit."""
        self.flush()

    def load(self) -> int:
        """Load memory state from disk, auto-detecting format.

        after loading from shards/legacy, replays any pending WAL
        entries so that a crash between ingest and flush loses no data.

        When ``use_sharding=False`` and ``sharding=True``, loads from
        the time-window shard files (``memories_YYYY-WXX.jsonl``).  Also
        auto-migrates ``memories.jsonl`` -> weekly shards if the flat file
        exists but no shard files are present.
        """
        if self.use_sharding and self.shard_manager:
            count = self._load_sharded()
            if count > 0:
                self._replay_wal()
                return len(self.memories)

        # time-window shard load path (only when sharded storage off)
        if not self.use_sharding and self._time_window_sharding and self._time_window_shards:
            self._auto_migrate_time_shards()
            count = self._load_time_window_shards()
            if count > 0:
                self._replay_wal()
                return len(self.memories)

        # Fall back to legacy flat format (memories.jsonl or memory_metadata.json)
        result = self._load_legacy()
        self._replay_wal()
        return len(self.memories)

    def _replay_wal(self) -> int:
        """Replay pending WAL entries that were not yet compacted to shards.

        This is called automatically on ``load()`` and is safe to call even
        when the WAL is empty or doesn't exist.

        Returns:
            Number of entries replayed.
        """
        pending = self._wal.load_pending()
        if not pending:
            return 0

        replayed = 0
        for entry_dict in pending:
            try:
                entry = MemoryEntry.from_dict(entry_dict)
            except Exception:
                continue   # corrupted entry - skip

            if entry.hash in self._hashes:
                continue   # already in memory (e.g. flushed before crash)

            self.memories.append(entry)
            self._hashes.add(entry.hash)
            replayed += 1

        if replayed:
            self.search_engine.mark_dirty()

        return replayed

    def _load_sharded(self) -> int:
        """Load from v0.4 sharded format."""
        if not self.shard_manager:
            return 0

        # Load all memories from shards (careful with memory usage)
        _LOAD_LIMIT = 75000
        self.memories = self.shard_manager.get_all_memories(limit=_LOAD_LIMIT)
        # Bug fix: warn when the safety limit is hit so users know data was truncated
        if len(self.memories) >= _LOAD_LIMIT:
            import warnings
            warnings.warn(
                f"parsica-spirit engine: loaded {_LOAD_LIMIT} memories (safety limit). "
                "Some older memories may not be in the active set. "
                "Consider calling forget() or archiving old shards.",
                UserWarning,
                stacklevel=3,
            )
        self._rebuild_content_indexes()
        if self.memories:
            self.search_engine.build_index(self.memories)
        return len(self.memories)

    def _load_legacy(self) -> int:
        """Load from v0.2/v0.3 legacy single-file format."""
        if not os.path.exists(self.legacy_metadata_path):
            return 0

        with open(self.legacy_metadata_path) as f:
            data = json.load(f)

        self.memories = [MemoryEntry.from_dict(d) for d in data.get("memories", [])]
        self._rebuild_content_indexes()
        if self.memories:
            self.search_engine.build_index(self.memories)
        return len(self.memories)

    # -- Time-Window Sharding helpers --------------------------------

    def _auto_migrate_time_shards(self) -> None:
        """Auto-migrate memories.jsonl -> weekly shards if flat file exists
        but no shard files are present yet (first-run migration).

        Emits a UserWarning so callers are aware the migration happened.
        """
        if self._time_window_shards is None:
            return

        flat_path = os.path.join(self.workspace, "memories.jsonl")
        existing_shards = self._time_window_shards.list_shards()

        if os.path.exists(flat_path) and not existing_shards:
            import warnings
            warnings.warn(
                "[parsica-spirit engine] Auto-migrating memories.jsonl to "
                "time-window shard files (memories_YYYY-WXX.jsonl). "
                "Original backed up as memories.jsonl.bak.",
                UserWarning,
                stacklevel=5,
            )
            _migrate_to_shards(self.workspace)

    def _load_time_window_shards(self) -> int:
        """Load memories from the active time-window shards.

        Loads current week + previous 2 weeks (~3-week window).
        When tiered_storage=True, classifies all shards into hot/warm/cold
        and loads only the hot tier into self.memories.  Warm shard names are
        stored in self._warm_shard_names for on-demand search fallback.
        """
        if not self._time_window_shards:
            return 0

        if self._tiered_storage and self._tier_manager is not None:
            # hot-only load
            all_shards = self._time_window_shards.list_shards()
            hot, warm, cold = self._tier_manager.classify_shards(all_shards)
            self._warm_shard_names = warm
            self._cold_shard_names = cold

            # Load hot shards only
            hot_entries: List[MemoryEntry] = []
            seen: set = set()
            for shard_name in hot:
                for entry in self._tier_manager._load_shard(shard_name):
                    if entry.hash not in seen:
                        hot_entries.append(entry)
                        seen.add(entry.hash)

            self.memories = hot_entries
            self._rebuild_content_indexes()
            if self.memories:
                self.search_engine.build_index(self.memories)

            # Snapshot shard hashes for incremental sync
            self._tier_manager.save_hashes(all_shards)
            return len(self.memories)

        # fallback: load current + 2 prior weeks
        self.memories = self._time_window_shards.load_active_shards(weeks=2)
        self._rebuild_content_indexes()
        if self.memories:
            self.search_engine.build_index(self.memories)
        return len(self.memories)

    # -- Tiered search helpers -------------------------------------

    def _warm_search_fallback(
        self,
        query: str,
        limit: int,
        category,
        min_confidence: float,
        decay_fn,
        session_id: str,
        effective_agent_id,
        memory_type,
        existing_hashes: set,
    ) -> list:
        """Load warm shards on-demand and search them.

        Called when the hot-tier search returns fewer than
        WARM_FALLBACK_THRESHOLD results. Results already present in the hot
        tier (by hash) are excluded to avoid duplicates.

        Returns a list of SearchResult objects (may be empty on any failure).
        """
        try:
            warm_entries = self._tier_manager.load_warm(self._warm_shard_names)
            if not warm_entries:
                return []
            # Build a temporary search engine scoped to warm entries
            from .search import SearchEngine
            warm_engine = SearchEngine()
            warm_engine.build_index(warm_entries)
            warm_results = warm_engine.search(
                query=query,
                memories=warm_entries,
                limit=limit * 5,
                category=category,
                min_score=min_confidence,
                decay_fn=decay_fn,
                session_id=session_id,
                agent_id=effective_agent_id,
            )
            # Filter memory_type and already-seen hashes
            if memory_type is not None:
                warm_results = [
                    r for r in warm_results
                    if getattr(r.entry, "memory_type", "episodic") == memory_type
                ]
            warm_results = [
                r for r in warm_results if r.entry.hash not in existing_hashes
            ]
            return warm_results[:limit]
        except Exception:
            logger.debug("Warm-tier search fallback failed", exc_info=True)
            return []

    def _cold_search(
        self,
        query: str,
        limit: int,
        decay_fn,
        effective_agent_id,
        existing_hashes: set,
    ) -> list:
        """Load cold shards on-demand and search them (explicit opt-in only).

        Returns a list of SearchResult objects (may be empty on any failure).
        """
        try:
            if not self._tier_manager or not self._cold_shard_names:
                return []
            cold_entries = self._tier_manager.load_warm(self._cold_shard_names)
            if not cold_entries:
                return []
            from .search import SearchEngine
            cold_engine = SearchEngine()
            cold_engine.build_index(cold_entries)
            cold_results = cold_engine.search(
                query=query,
                memories=cold_entries,
                limit=limit * 5,
                decay_fn=decay_fn,
                agent_id=effective_agent_id,
            )
            return [r for r in cold_results if r.entry.hash not in existing_hashes][:limit]
        except Exception:
            logger.debug("Cold-tier search failed", exc_info=True)
            return []

    # -- Incremental Index Loading helpers -------------------------

    def _setup_incremental_index(self) -> None:
        """Set up the BM25 index for fast restarts.

        Strategy:
        1. Try to load a saved index from ``.parsica_index.json``.
        2. On failure (missing / corrupt), do a full ``build_index()`` (if
           memories exist) and save the result.  An empty-corpus save is
           written when there are no memories so the file always exists
           after init - enabling WAL delta application on the next restart.
        3. Apply any pending WAL entries from ``.parsica_wal.jsonl`` to
           fold in memories ingested since the last save.
        """
        loaded = self.search_engine.load_index(self._index_path)

        if not loaded:
            # Full rebuild from current memories (or empty corpus)
            if self.memories:
                self.search_engine.build_index(self.memories)
            self.search_engine.save_index(self._index_path)
        else:
            # Index loaded OK - apply WAL delta to catch up
            reader = WALReader()
            reader.open(self._index_wal_path)
            wal_dicts = reader.read_all()
            if wal_dicts:
                from .entry import MemoryEntry as _ME
                wal_entries = []
                for d in wal_dicts:
                    try:
                        wal_entries.append(_ME.from_dict(d))
                    except Exception:
                        pass
                if wal_entries:
                    self.search_engine.apply_wal_entries(wal_entries)

    def _flush_incremental_index(self) -> None:
        """Rebuild the full index, save it, and clear the index WAL.

        Called automatically when the index WAL reaches 500 entries.
        """
        self.search_engine.build_index(self.memories)
        self.search_engine.save_index(self._index_path)
        # Clear the WAL file
        reader = WALReader()
        reader.open(self._index_wal_path)
        reader.clear()
        # Reopen the writer on the (now empty) file
        self._index_wal_writer.close()
        self._index_wal_writer = WALWriter()
        self._index_wal_writer.open(self._index_wal_path)
        self._index_wal_entry_count = 0

    # -- search with indexing -------------------------------------------

    def search(
        self,
        query: str,
        limit: int = 20,
        tags: Optional[List[str]] = None,
        tag_mode: str = "any",
        date_range: Optional[Tuple[str, str]] = None,
        use_decay: bool = True,
        category: str = None,
        min_confidence: float = 0.0,
        sentiment_filter: str = None,
        memory_type: Optional[str] = None,
        explain: bool = False,
        session_id: str = "*",               # session isolation; "*" = all sessions (cross-session search for recall). Session isolation is for writes, not reads.
        agent_id: Optional[str] = None,      # agent-scoped search
        include_cold: bool = False,          # include cold-tier shards
        cross_session_recall: str = "all",   # "all" = no filter, "semantic" = other sessions only get semantic memories, "none" = strict session isolation
        confidence_floor: float = 0.0,      # minimum confidence to include
        source_channel: Optional[str] = None,  # filter by channel (e.g. "chatA:room1", "chatB:thread2")
    ) -> list:
        """Search memories using BM25-inspired ranking with optional fast indexes.

        results are served from the LRU read-cache when the query
        signature matches a previously seen search.  On a cache miss the full
        BM25 pipeline runs and the result-set is cached for future calls.
        Access counts are incremented for every returned entry so that hot
        memories receive a relevance boost in future searches.

        Bug fix: BM25 IDF is always computed over the full corpus (self.memories)
        so that type/sentiment filters do not corrupt IDF weights.  Filters are
        applied AFTER scoring.

        Feature: recall_priority is applied to each scored entry so that e.g.
        mistake memories (recall_priority=1.0) float to the top. Canonical types
        read MEMORY_TYPE_CONFIGS; a custom type reads the sanitised type_config
        riding the entry (see _entry_recall_priority).

        Args:
            query: Search query string.
            limit: Maximum results.
            memory_type: Optional filter - only return entries of this type.
            explain: If True, return SearchResult objects with score explanations.
        """
        _t0 = time.monotonic()

        # -- cache key ---------------------------------------------------
        # include session_id in cache key so sessions don't share results
        # effective_agent_id - use explicitly passed agent_id, else fall
        # back to the MemorySystem's own identity (self.agent_name).  None means
        # no scoping (shared pool visible to all).
        effective_agent_id = agent_id if agent_id is not None else self.agent_name

        cache_key = json.dumps(
            [query, limit, tags, tag_mode, date_range, use_decay,
             category, min_confidence, sentiment_filter, memory_type, explain,
             session_id, effective_agent_id, cross_session_recall, source_channel,
             confidence_floor, include_cold],
            sort_keys=True, default=str,
        )

        if self._read_cache is not None:
            cached = self._read_cache.get(cache_key)
            if cached is not None:
                self._perf.record_search((time.monotonic() - _t0) * 1000)
                return cached

        # -- full search pipeline -----------------------------------------
        # Always build the BM25 index from the FULL corpus
        # so that IDF weights are never corrupted by a filtered subset.
        if self.search_engine._doc_count != len(self.memories):
            self.search_engine.build_index(self.memories)

        decay_fn = self.decay.score if use_decay else None

        # Vocabulary bridging happens on the DOCUMENT side: enrichment fields
        # (search_keywords 2x, enriched_summary 2x, search_queries 3x) are
        # weighted into the index at write time, keeping short queries sharp
        # against the inverted-index pre-filter and BM25 scoring.

        # When cross_session_recall is active and not "all", we need to
        # search across ALL sessions first, then filter. Otherwise the search
        # engine's session_id pre-filter would exclude other sessions' memories
        # before our cross-session filter can selectively include semantic ones.
        search_session_id = session_id
        if cross_session_recall != "all" and session_id and session_id != "*":
            search_session_id = "*"  # search all, filter post-scoring

        # Fetch a generous pool from the full corpus, then filter & trim.
        # Using limit * 10 (min 200) ensures filters don't starve results.
        fetch_limit = max(limit * 10, 200)
        search_results = self.search_engine.search(
            query=query,
            memories=self.memories,
            limit=fetch_limit,
            category=category,
            min_score=min_confidence,
            decay_fn=decay_fn,
            session_id=search_session_id,
            agent_id=effective_agent_id,   # pre-filter by agent
            index_manager=self.index_manager if self.use_indexing else None,
        )

        # -- post-scoring filters (applied AFTER BM25 scoring) --
        if sentiment_filter:
            search_results = [
                r for r in search_results
                if hasattr(r.entry, "sentiment")
                and r.entry.sentiment
                and sentiment_filter in r.entry.sentiment
            ]
        if memory_type is not None:
            search_results = [
                r for r in search_results
                if getattr(r.entry, "memory_type", "episodic") == memory_type
            ]
        # tags / date_range post-scoring filters, same place & style as the
        # sentiment/memory_type filters above. tag_mode "all" = entry carries
        # every requested tag (AND); "any" (default) = at least one (OR),
        # mirroring TagIndex.get_memories_by_tags. date_range=(start, end) is
        # inclusive over the creation DATE (created[:10], YYYY-MM-DD), matching
        # DateIndex.get_memories_in_range.
        if tags:
            _want = set(tags)
            if tag_mode == "all":
                search_results = [
                    r for r in search_results
                    if _want <= set(getattr(r.entry, "tags", []) or [])
                ]
            else:  # "any"
                search_results = [
                    r for r in search_results
                    if _want & set(getattr(r.entry, "tags", []) or [])
                ]
        if date_range:
            _start, _end = date_range
            search_results = [
                r for r in search_results
                if _start <= (getattr(r.entry, "created", "") or "")[:10] <= _end
            ]

        # -- recall_priority boost -----------------------------
        for r in search_results:
            r.score *= self._entry_recall_priority(r.entry)

        # Re-sort after recall_priority adjustment
        search_results.sort(key=lambda r: r.score, reverse=True)
        search_results = search_results[:limit * 3 if source_channel else limit]

        # -- Warm-tier fallback ------------------------------------
        # If hot-tier search returned fewer than WARM_FALLBACK_THRESHOLD
        # results AND warm shards exist, load them on-demand and merge.
        if (
            self._tiered_storage
            and self._tier_manager is not None
            and self._warm_shard_names
            and len(search_results) < 3
        ):
            warm_extra = self._warm_search_fallback(
                query=query,
                limit=limit,
                category=category,
                min_confidence=min_confidence,
                decay_fn=decay_fn,
                session_id=session_id,
                effective_agent_id=effective_agent_id,
                memory_type=memory_type,
                existing_hashes={r.entry.hash for r in search_results},
            )
            if warm_extra:
                search_results = sorted(
                    search_results + warm_extra,
                    key=lambda r: r.score,
                    reverse=True,
                )[:limit]

        # -- Cold-tier search (explicit opt-in only) ---------------
        if include_cold and self._tiered_storage and self._cold_shard_names and self._tier_manager:
            cold_extra = self._cold_search(
                query=query,
                limit=limit,
                decay_fn=decay_fn,
                effective_agent_id=effective_agent_id,
                existing_hashes={r.entry.hash for r in search_results},
            )
            if cold_extra:
                search_results = sorted(
                    search_results + cold_extra,
                    key=lambda r: r.score,
                    reverse=True,
                )[:limit]

        # Reinforce accessed memories & record access counts
        for r in search_results:
            self.decay.reinforce(r.entry)
            self._record_access(r.entry.hash)   # access tracking

        # Apply access-count boost and re-sort
        if search_results:
            boosted = []
            for r in search_results:
                boost = self._access_tracker.boost_score(r.entry.hash)
                boosted.append((r, r.score * boost))
            boosted.sort(key=lambda x: x[1], reverse=True)
            search_results = [r for r, _ in boosted]

        # -- Graph-based relevance boost ---------------------------
        # Apply graph boost only when graph is enabled AND has been built.
        # This re-ranks results using entity relationship signals without
        # changing which entries are returned (no cold/warm tier loading).
        if (self._graph_enabled and self._graph is not None
                and search_results and not include_cold):
            try:
                search_results = self._graph.boost_results(query, search_results)
            except Exception:
                logger.debug("Graph boost failed for query %r", query[:50], exc_info=True)

        # -- Confidence threshold filtering -------------------------
        # Filter results below confidence floor (enables "I don't know" capability)
        if confidence_floor > 0:
            filtered_results = []
            for r in search_results:
                entry_confidence = getattr(r.entry, 'confidence', 0.5)
                score = getattr(r, 'score', 0.0) if hasattr(r, 'score') else getattr(r, 'relevance', 0.0)
                if (entry_confidence * score) >= confidence_floor:
                    filtered_results.append(r)
            search_results = filtered_results

        # -- source_channel filter ------------------------------------
        if source_channel:
            search_results = [
                r for r in search_results
                if getattr(r.entry if hasattr(r, 'entry') else r, 'source_channel', '') == source_channel
            ][:limit]

        if explain:
            # cross-session filter for explain mode (SearchResult objects)
            if cross_session_recall != "all" and session_id and session_id != "*":
                filtered_sr = []
                for r in search_results:
                    entry_session = getattr(r.entry, "session_id", "") or ""
                    is_own = (entry_session == session_id) or (not entry_session)
                    if is_own:
                        filtered_sr.append(r)
                    elif cross_session_recall == "none":
                        continue
                    elif cross_session_recall == "semantic":
                        if getattr(r.entry, "memory_type", "episodic") == "semantic":
                            filtered_sr.append(r)
                search_results = filtered_sr
            self._perf.record_search((time.monotonic() - _t0) * 1000)
            if self._read_cache is not None:
                self._read_cache.put(cache_key, search_results)
            return search_results

        # Backward compat: return MemoryEntry with search-time confidence.
        # Uses copy to avoid mutating the canonical entry in the index.
        # WindowEntry (sliding-window entries) are resolved back to their
        # constituent MemoryEntry objects for API compatibility.
        # Agent scoping is preserved: only entries matching the effective
        # agent_id (or shared entries) are included in the resolved set.
        entries = []
        seen_hashes: set = set()
        _mem_by_hash = {m.hash: m for m in self.memories}
        for r in search_results:
            entry = r.entry
            # Resolve WindowEntry -> constituent MemoryEntries
            if hasattr(entry, "source_ids"):
                for h in entry.source_ids:
                    if h in seen_hashes:
                        continue
                    mem_entry = _mem_by_hash.get(h)
                    if mem_entry:
                        # Respect agent scoping: skip entries from other agents
                        entry_aid = getattr(mem_entry, "agent_id", None)
                        if (effective_agent_id and entry_aid
                                and entry_aid != effective_agent_id):
                            continue
                        entry_copy = copy.copy(mem_entry)
                        entry_copy.confidence = r.relevance
                        entries.append(entry_copy)
                        seen_hashes.add(h)
            else:
                if entry.hash not in seen_hashes:
                    entry_copy = copy.copy(entry)
                    entry_copy.confidence = r.relevance
                    entries.append(entry_copy)
                    seen_hashes.add(entry.hash)

        # -- Cross-session recall filtering -------------------------
        # Applied AFTER WindowEntry resolution so individual MemoryEntry objects
        # are correctly filtered (WindowEntries bundle multiple sessions).
        #   "semantic" - other sessions' memories only if memory_type == "semantic"
        #   "none"     - only memories from the current session (strict isolation)
        #   "all"      - no filtering (default, backward compatible)
        if cross_session_recall != "all" and session_id and session_id != "*":
            filtered_entries = []
            for entry in entries:
                entry_session = getattr(entry, "session_id", "") or ""
                is_own_session = (entry_session == session_id) or (not entry_session)
                if is_own_session:
                    filtered_entries.append(entry)
                elif cross_session_recall == "none":
                    continue
                elif cross_session_recall == "semantic":
                    entry_type = getattr(entry, "memory_type", "episodic") or "episodic"
                    if entry_type == "semantic":
                        filtered_entries.append(entry)
            entries = filtered_entries

        elapsed_ms = (time.monotonic() - _t0) * 1000
        self._perf.record_search(elapsed_ms)
        if self._read_cache is not None:
            self._read_cache.put(cache_key, entries)
        return entries
    
    def recent(
        self,
        limit: int = 10,
        agent_id: Optional[str] = None,
        include_shared: bool = True,
    ) -> list:
        """Return the N most recently accessed memories for this agent.

        Unlike search(), this method does NOT use BM25 - it is a pure
        timestamp sort.  Guaranteed to return results as long as any
        memories exist.  Use this for session restore where you want
        "what was I working on?" rather than keyword matching.

        added for recency-first session restore.

        Args:
            limit: Maximum number of entries to return.
            agent_id: Filter to this agent.  Defaults to self.agent_name.
                      Entries with no agent_id are the shared pool and are
                      always included when include_shared=True.
            include_shared: Include memories with no agent_id (shared pool).

        Returns:
            List of MemoryEntry objects, newest first.
        """
        effective_agent_id = agent_id if agent_id is not None else self.agent_name

        if effective_agent_id:
            filtered = [
                m for m in self.memories
                if getattr(m, "agent_id", None) == effective_agent_id
                or (include_shared and not getattr(m, "agent_id", None))
            ]
        else:
            filtered = list(self.memories)

        # Sort by last_accessed desc, fall back to created_at
        def _sort_key(m):
            ts = getattr(m, "last_accessed", None) or getattr(m, "created_at", None) or ""
            return ts

        filtered.sort(key=_sort_key, reverse=True)
        return filtered[:limit]

    # -- cross-channel recency recall --------------------------

    def recall_recent(
        self,
        hours: float = 6.0,
        limit: int = 5,
        exclude_channel: Optional[str] = None,
        session_id: Optional[str] = None,
        source_channel: Optional[str] = None,
    ) -> List:
        """Retrieve the most recent memories, optionally filtered by session/channel.

        This complements semantic search by surfacing *temporally recent*
        memories regardless of keyword or embedding relevance - perfect
        for cross-channel continuity where a user switches apps.

        Parameters
        ----------
        hours : float
            How far back to look from now (default 6).
        limit : int
            Max memories to return (default 5).
        exclude_channel : str, optional
            Channel identifier to skip (e.g. the current channel, which
            is already covered by semantic search).
        session_id : str, optional
            Filter to only memories from this session. None = all sessions.
        source_channel : str, optional
            Filter to only memories from this channel. None = all channels.

        Returns
        -------
        list[MemoryEntry]
            Newest-first list of memories, capped at *limit*.
        """
        from .search import search_by_recency
        results = search_by_recency(
            self.memories,
            hours=hours,
            limit=limit * 3 if (session_id or source_channel) else limit,  # over-fetch for post-filter
            exclude_channel=exclude_channel,
        )
        # post-filter by session_id and source_channel
        if session_id:
            results = [m for m in results if getattr(m, 'session_id', '') == session_id]
        if source_channel:
            results = [m for m in results if getattr(m, 'source_channel', '') == source_channel]
        return results[:limit]

    # -- ingestion -------------------------------------------------------

    # -- Session isolation: write gate + lifecycle API ---------------------

    def _check_write_gate(self, session_id: Optional[str], operation: str = "") -> bool:
        """Check whether a write operation is allowed for this session.

        Three ``session_awareness`` modes:

        - ``"off"`` (default): no gate, always allow - full backward compat.
        - ``"open"``: writes always allowed; idle/unregistered writes emit a
          warning but proceed. The engine's own dedup remains the guard
          against pollution.
        - ``"closed"``: only ACTIVE sessions may write. Idle or unregistered
          sessions are blocked - activate the session via ``begin_turn()``
          first.

        Returns:
            True if the write is allowed, False if blocked.
        """
        if self._session_awareness == "off" or self._session_tracker is None:
            return True
        allowed = self._session_tracker.is_write_allowed(session_id)
        if not allowed:
            logger.warning(
                "%s mode: write blocked - session=%s is idle or unregistered "
                "(op=%s). Call begin_turn() to activate the session.",
                self._session_awareness.upper(), sid_tag(session_id), operation,
            )
        return allowed

    def begin_turn(self, session_id: str) -> None:
        """Signal that a session has received an inbound message and is active.

        The ONLY way a session becomes ACTIVE in "closed" mode. Idempotent -
        nested/concurrent turns are reference-counted.
        No-op when session_awareness="off".
        """
        if self._session_tracker is not None:
            self._session_tracker.begin_turn(session_id)

    def end_turn(self, session_id: str) -> None:
        """Signal that the runtime has finished processing the current turn.

        The session transitions to IDLE only when all nested turns are
        complete AND all pending sub-agent tasks have been reported via
        complete_task(). No-op when session_awareness="off".
        """
        if self._session_tracker is not None:
            self._session_tracker.end_turn(session_id)

    def add_task(self, session_id: str, task_name: Optional[str] = None) -> str:
        """Register a pending sub-agent task for a session.

        The session stays ACTIVE (even after end_turn) while tasks are
        pending. Returns the task name to pass to complete_task(), or ""
        when rejected or session_awareness="off".
        """
        if self._session_tracker is not None:
            import uuid as _uuid
            _name = task_name or f"task-{_uuid.uuid4().hex[:8]}"
            accepted = self._session_tracker.add_task(session_id, _name)
            if accepted is True or accepted is None:
                return _name
            return ""
        return ""

    def complete_task(self, session_id: str, task_name: Optional[str] = None) -> None:
        """Mark a pending sub-agent task as complete.

        When all pending tasks are complete AND end_turn() has been called,
        the session transitions to IDLE. No-op when session_awareness="off"
        or task_name is None.
        """
        if self._session_tracker is not None and task_name is not None:
            self._session_tracker.complete_task(session_id, task_name)

    def force_idle_session(self, session_id: str) -> None:
        """Escape hatch: force a session IDLE regardless of turn depth or
        pending tasks (e.g. a sub-agent crashed without reporting back).
        No-op when session_awareness="off".
        """
        if self._session_tracker is not None:
            self._session_tracker.force_idle(session_id)

    def ingest(
        self,
        content: str,
        source: str = "inline",
        category: str = "general",
        memory_type: str = DEFAULT_TYPE,
        type_config: Optional[Dict] = None,
        tags: Optional[List[str]] = None,
        agent_id: Optional[str] = None,
        session_id: Optional[str] = None,
        channel_id: Optional[str] = None,
        source_url: Optional[str] = None,
        content_hash: Optional[str] = None,
        source_channel: Optional[str] = None,
    ) -> int:
        """Ingest raw text. Returns count of new memories added.

        accepts optional *memory_type* and *type_config* to attach
        structured type information to each entry.  Backward-compatible -
        existing callers that don't pass these get episodic defaults.

        Args:
            content: Raw text to ingest (multi-line supported).
            source: Source label (file path, "inline", etc.).
            category: Category label (general, strategic, etc.).
            memory_type: One of the canonical types or a custom string.
                Defaults to "episodic".
            type_config: For custom (non-canonical) types only, a dict with
                optional keys decay_multiplier, importance_boost,
                recall_priority. Ignored when *memory_type* is one of the
                canonical types in MEMORY_TYPE_CONFIGS (their configs are
                fixed by design).
            tags: Optional explicit tags to merge with auto-extracted tags.
        """
        # Session isolation - block idle/unregistered sessions from writing.
        if not self._check_write_gate(session_id, "ingest"):
            return 0

        # Metadata scrub gate: redact secrets + PII from every metadata surface
        # that reaches disk (labels, ids, provenance, channel, tags, type_config)
        # BEFORE any MemoryEntry is constructed. Content is scrubbed per-line
        # below. Scrubbed AFTER the write gate so the gate still sees the raw
        # session_id it was registered with. All scrubs are idempotent.
        source = self._scrub_str(source)
        category = self._scrub_str(category)
        agent_id = self._scrub_str(agent_id)
        session_id = self._scrub_str(session_id)
        source_url = self._scrub_str(source_url)
        content_hash = self._scrub_str(content_hash)
        source_channel = self._scrub_str(source_channel)
        channel_id = self._scrub_str(channel_id)
        tags = self._scrub_tag_list(tags)
        # Ingress gateway: normalise the type and reduce type_config to allowlisted,
        # bounded, scrubbed keys BEFORE it reaches the entry, the config, or disk. The
        # reassigned memory_type is what the MemoryEntry below stores (not the raw arg).
        memory_type, type_config = self._accept_ingress(memory_type, type_config)

        cfg = get_type_config(memory_type, type_config)
        count = 0
        for i, line in enumerate(content.split("\n")):
            stripped = line.strip()
            if len(stripped) < 15 or stripped.startswith("```") or stripped == "---":
                continue
            # reject raw JSON metadata lines (conversation_label, sender_id etc.)
            if _METADATA_LINE_RE.match(stripped):
                continue
            
            # Pipeline ingest quality gate (conversation noise only)
            _is_pipeline = source.startswith('pipeline:') or source == 'agent_turn'
            if _is_pipeline:
                if len(stripped) < 80:
                    continue  # raised from 40 - short fragments aren't memories
                # reject conversation noise (acknowledgments, status messages)
                if _CONVERSATION_NOISE_RE.match(stripped):
                    continue
                # Skip pure code lines (indented code, variable assignments)
                if _CODE_LINE_RE.match(stripped):
                    continue
                # Skip table formatting
                if stripped.startswith('|') and '---' in stripped:
                    continue
                if stripped.count('|') > 3 and len(stripped.split()) < 5:
                    continue
            
            # Scrub gate: redact secrets and PII in-place before storing
            # (parity with the Context facade - the engine never stores a raw secret or PII).
            stripped, _ = scrub(stripped, scrub_pii=True)

            # Normalize content for dedup: strip Turn/INPUT/OUTPUT prefixes
            # so the same message stored as "Turn: X" and "INPUT: X" deduplicates.
            _content_for_dedup = stripped
            if _is_pipeline:
                _content_for_dedup = _TURN_PREFIX_RE.sub('', stripped).strip()

            entry = MemoryEntry(stripped, source, i + 1, category,
                                memory_type=memory_type,
                                agent_id=agent_id or self.agent_name,
                                session_id=session_id or "")
            # stamp ingestion provenance
            if source_url is not None:
                entry.source_url = source_url
            if content_hash is not None:
                entry.content_hash = content_hash
            # cross-channel recency recall
            _chan = source_channel or channel_id   # callers may forward either; honor both
            if _chan:
                entry.source_channel = _chan
            if entry.hash in self._hashes:
                continue

            # Content-normalized dedup guard
            # Uses prefix-stripped content for pipeline sources
            _content_norm = _content_for_dedup.lower().strip()
            if _content_norm in self._content_norms:
                continue

            # Process through gating system
            gate_priority = self.gating.classify(stripped)
            if gate_priority == "P3":  # Skip noise
                continue

            entry.tags = self._extract_tags(stripped)
            # Merge caller-supplied tags with auto-extracted tags
            if tags:
                entry.tags = sorted(set(entry.tags) | set(tags))

            # CategoryTagger auto-tag + auto-categorise on ingest
            cat_tags = _ct.tag(stripped)
            for ct_tag in cat_tags:
                if ct_tag not in entry.tags:
                    entry.tags.append(ct_tag)
            # Only overwrite category if caller left it at default "general"
            if entry.category == "general" and cat_tags:
                entry.category = _ct.primary_category(stripped)

            entry.sentiment = self.sentiment.analyze(stripped)

                # LLM enrichment (non-fatal, optional)
            # toggle wiring: respect runtime flags when present.
            # These are set by the embedding host at runtime.
            # Default behaviour stays the same for direct library users:
            # enrichment ON, decomposition OFF.
            enrichment = None  # Initialize to ensure variable exists
            enrichment_enabled = getattr(self, "_enrichment_enabled", True)
            decomposition_enabled = getattr(self, "_decomposition_enabled", False)
            if enrichment_enabled and self._enricher is not None:
                try:
                    enrichment = self._enricher(stripped)
                    if enrichment and isinstance(enrichment, dict):
                        # Scrub the enricher output BEFORE any field is copied,
                        # fed to co-occurrence, or decomposed - the enricher is
                        # an external callable and can echo secrets/PII.
                        enrichment = self._scrub_enrichment_dict(enrichment)
                        if enrichment.get("tags"):
                            entry.tags = sorted(
                                set(entry.tags) | {t.lower() for t in enrichment["tags"]}
                            )
                        if enrichment.get("summary"):
                            entry.enriched_summary = str(enrichment["summary"])
                        if enrichment.get("keywords"):
                            entry.search_keywords = [
                                str(k).lower() for k in enrichment["keywords"]
                            ]
                        if enrichment.get("search_queries"):
                            entry.search_queries = [
                                str(q) for q in enrichment["search_queries"]
                            ]
                        # Feed enrichment relationships into co-occurrence index
                        self._feed_cooccurrence_from_enrichment(stripped, enrichment)
                        self._enrichment_count += 1
                except Exception:
                    logger.debug("LLM enrichment failed for entry", exc_info=True)
                    enrichment = None

            # Fact decomposition - store atomic facts as separate entries
            if decomposition_enabled and self._enricher is not None and enrichment and isinstance(enrichment, dict):
                count += self._decompose_facts(
                    enrichment, entry, source, category,
                    session_id or "", agent_id,
                )

            # Apply type importance boost
            boost = cfg.get("importance_boost", 1.0)
            if boost != 1.0:
                entry.importance = min(entry.importance * boost, self.decay.max_score)

            # Store type_config for custom types
            if type_config:
                entry.type_metadata["_type_config"] = type_config

            self.memories.append(entry)
            self._hashes.add(entry.hash)
            _cn = _TURN_PREFIX_RE.sub('', entry.content).strip().lower()
            self._content_norms.add(_cn)
            self.search_engine.mark_dirty()

            if self.use_indexing and self.index_manager:
                self.index_manager.add_memory(entry)

            # WAL append
            self._wal.append(entry.to_dict())

            # Index into memory graph (non-fatal)
            if self._graph_enabled and self._graph is not None:
                try:
                    self._graph.index_entry(entry.hash, entry.content)
                except Exception:
                    logger.debug("Graph index_entry failed for %s", entry.hash[:12], exc_info=True)

            # Write to time-window shard (when sharded storage off)
            if not self.use_sharding and self._time_window_sharding and self._time_window_shards:
                self._time_window_shards.write_entry(entry)

            # Append to index WAL for incremental index tracking
            try:
                self._index_wal_writer.append(entry.to_dict())
                self._index_wal_entry_count += 1
            except Exception:
                logger.debug("Index WAL append failed; will rebuild on restart", exc_info=True)

            # Update local co-occurrence index
            self._update_cooccurrence(stripped)

            count += 1

        # Invalidate read-cache on write
        if count and self._read_cache is not None:
            self._read_cache.invalidate()

        # Auto-flush if WAL threshold is reached
        if self._wal.should_flush():
            self.flush()

        # Flush index WAL when it reaches 500 entries
        if self._index_wal_entry_count >= 500:
            self._flush_incremental_index()

        return count

    def ingest_file(self, file_path: str, category: str = "tactical") -> int:
        """Ingest a single file."""
        if not os.path.exists(file_path):
            return 0
        
        try:
            with open(file_path, encoding="utf-8") as f:
                content = f.read()
            return self.ingest(content, source=file_path, category=category)
        except (OSError, UnicodeDecodeError):
            return 0

    def ingest_directory(self, dir_path: str, category: str = "tactical",
                        pattern: str = "*.md") -> int:
        """Ingest all matching files in a directory."""
        if not os.path.exists(dir_path):
            return 0
        
        total = 0
        for path in Path(dir_path).glob(pattern):
            if path.is_file():
                total += self.ingest_file(str(path), category)
        return total

    def ingest_bulk(
        self,
        items: List[str],
        source: str = "bulk",
        category: str = "general",
        memory_type: str = DEFAULT_TYPE,
        tags: Optional[List[str]] = None,
        agent_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> int:
        """Ingest a list of strings in a single deferred-flush pass.

        For large-scale ingest (1K+), this is significantly faster
        than calling ingest() in a loop because it raises the WAL flush
        threshold for the duration of the batch and does exactly one flush
        at the end. At 10K entries this is ~6x faster than loop ingest.

        The flush threshold is restored to its original value after the batch
        even if an exception is raised mid-batch (try/finally).

        Args:
            items: List of content strings to ingest (each treated as one entry).
            source: Source label applied to all entries.
            category: Category label applied to all entries.
            memory_type: Memory type applied to all entries.
            tags: Optional tags merged with auto-extracted tags on each entry.
            agent_id: Optional agent scope override.
            session_id: Optional session scope override.

        Returns:
            Total number of new memories added.
        """
        if not items:
            return 0

        # Defer WAL flush for the duration of the batch (only if WAL supports it).
        # Guard with hasattr so mocked/stubbed WALs in tests don't raise AttributeError
        # before the try block, which would skip the finally and leave flush_interval unrestored.
        _has_flush_control = hasattr(self._wal, 'flush_interval')
        original_flush_interval = self._wal.flush_interval if _has_flush_control else None
        if _has_flush_control:
            # Set flush interval high enough to cover the entire batch
            self._wal.flush_interval = len(items) + 1
        total = 0
        try:
            for item in items:
                total += self.ingest(
                    item,
                    source=source,
                    category=category,
                    memory_type=memory_type,
                    tags=tags,
                    agent_id=agent_id,
                    session_id=session_id,
                )
        finally:
            if _has_flush_control and original_flush_interval is not None:
                self._wal.flush_interval = original_flush_interval
            try:
                self.flush()
            except Exception:
                pass  # WAL entries are safe; flush will be retried on next auto-flush

        return total

    # -- typed ingest methods ----------------------------------

    def ingest_mistake(
        self,
        what_happened: str,
        correction: str,
        root_cause: Optional[str] = None,
        severity: str = "medium",
        tags: Optional[List[str]] = None,
        source: str = "mistake",
        agent_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> Optional[MemoryEntry]:
        """Ingest a structured mistake memory.

        Mistakes receive 2x importance, 10x half-life, and are surfaced
        proactively in context packets via the "Known Pitfalls" section.

        Args:
            what_happened: Brief description of what went wrong.
            correction: What should be done instead.
            root_cause: Why it happened (optional but strongly recommended).
            severity: "high", "medium", or "low" (default "medium").
            tags: Extra tags to attach.
            source: Source label (default "mistake").

        Returns:
            The created MemoryEntry, or None if duplicate.
        """
        # Session isolation - block idle/unregistered sessions from writing.
        if not self._check_write_gate(session_id, "ingest_mistake"):
            return None

        if severity not in SEVERITY_LEVELS:
            severity = "medium"

        content = format_mistake_content(what_happened, correction, root_cause, severity)
        # Scrub gate: redact secrets and PII in-place before storing (parity with the Context facade).
        content, _ = scrub(content, scrub_pii=True)
        # Metadata scrub gate: scrub each structured field BEFORE it is stored.
        # The type_metadata dict below is built from these SCRUBBED values, not
        # the raw args, so raw secrets/PII never persist via type_metadata.
        what_happened = self._scrub_str(what_happened)
        correction = self._scrub_str(correction)
        root_cause = self._scrub_str(root_cause)
        source = self._scrub_str(source)
        agent_id = self._scrub_str(agent_id)
        session_id = self._scrub_str(session_id)
        tags = self._scrub_tag_list(tags)
        cfg = MEMORY_TYPE_CONFIGS["mistake"]

        entry = MemoryEntry(content, source, 0, "mistake", memory_type="mistake",
                            agent_id=agent_id or self.agent_name,
                            session_id=session_id or "")
        if entry.hash in self._hashes:
            return None
        _cn = _TURN_PREFIX_RE.sub('', entry.content).strip().lower()
        if _cn in self._content_norms:  # catch content-norm dupes missed by hash check
            return None

        entry.importance = min(1.0 * cfg["importance_boost"], self.decay.max_score)
        entry.tags = list(set(["mistake", f"severity:{severity}"] + (tags or [])))
        entry.sentiment = self.sentiment.analyze(content)
        entry.type_metadata = {
            "what_happened": what_happened,
            "correction": correction,
            "root_cause": root_cause,
            "severity": severity,
        }

        self.memories.append(entry)
        self._hashes.add(entry.hash)
        self._content_norms.add(_cn)  # _cn computed above before dup checks
        self.search_engine.mark_dirty()

        if self.use_indexing and self.index_manager:
            self.index_manager.add_memory(entry)

        # WAL
        self._wal.append(entry.to_dict())
        if self._read_cache is not None:
            self._read_cache.invalidate()
        if self._wal.should_flush():
            self.flush()

        return entry

    def ingest_fact(
        self,
        content: str,
        source: str = "fact",
        tags: Optional[List[str]] = None,
        category: str = "general",
        agent_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> int:
        """Ingest a fact-type memory (normal decay, high recall priority)."""
        return self._ingest_typed(
            content, source, category, "fact", tags=tags,
            agent_id=agent_id, session_id=session_id,
        )

    def ingest_preference(
        self,
        content: str,
        source: str = "preference",
        tags: Optional[List[str]] = None,
        category: str = "general",
        agent_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> int:
        """Ingest a preference-type memory (3x slower decay, high context-matched recall)."""
        return self._ingest_typed(
            content, source, category, "preference", tags=tags,
            agent_id=agent_id, session_id=session_id,
        )

    def ingest_procedure(
        self,
        content: str,
        source: str = "procedure",
        tags: Optional[List[str]] = None,
        category: str = "general",
        agent_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> int:
        """Ingest a procedure-type memory (3x slower decay, high task-matched recall)."""
        return self._ingest_typed(
            content, source, category, "procedure", tags=tags,
            agent_id=agent_id, session_id=session_id,
        )

    def _ingest_typed(
        self,
        content: str,
        source: str,
        category: str,
        memory_type: str,
        tags: Optional[List[str]] = None,
        agent_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> int:
        """Helper: ingest one or more lines with a specific memory type.

        Applies the same P0-P3 input gating that ``ingest()``
        uses so that P3 (ephemeral) content is never silently stored via the
        typed-ingest helpers.
        """
        # Session isolation - block idle/unregistered sessions from writing.
        if not self._check_write_gate(session_id, "_ingest_typed"):
            return 0

        memory_type = normalize_memory_type(memory_type)   # defensive: the type reaches the entry
        cfg = get_type_config(memory_type)
        # Metadata scrub gate: redact secrets + PII from metadata surfaces
        # BEFORE any MemoryEntry is constructed (content is scrubbed per-line
        # below). Scrubbed after the write gate so it still sees the raw session_id.
        source = self._scrub_str(source)
        category = self._scrub_str(category)
        agent_id = self._scrub_str(agent_id)
        session_id = self._scrub_str(session_id)
        tags = self._scrub_tag_list(tags)
        count = 0
        for i, line in enumerate(content.split("\n")):
            stripped = line.strip()
            if len(stripped) < 15 or stripped.startswith("```") or stripped == "---":
                continue
            # reject raw JSON metadata lines
            if _METADATA_LINE_RE.match(stripped):
                continue

            # Scrub gate: redact secrets and PII in-place before storing (parity with the Context facade).
            stripped, _ = scrub(stripped, scrub_pii=True)

            # apply gating - drop P3 noise
            if not self.gating.should_store(stripped):
                continue

            entry = MemoryEntry(stripped, source, i + 1, category,
                                memory_type=memory_type,
                                agent_id=agent_id or self.agent_name,
                                session_id=session_id or "")
            if entry.hash in self._hashes:
                continue
            _cn = _TURN_PREFIX_RE.sub('', entry.content).strip().lower()
            if _cn in self._content_norms:  # catch content-norm dupes missed by hash check
                continue

            entry.importance = min(1.0 * cfg.get("importance_boost", 1.0),
                                   self.decay.max_score)
            entry.tags = list(set([memory_type] + self._extract_tags(stripped) + (tags or [])))

            # CategoryTagger auto-tag + auto-categorise on ingest
            cat_tags = _ct.tag(stripped)
            for ct_tag in cat_tags:
                if ct_tag not in entry.tags:
                    entry.tags.append(ct_tag)
            if entry.category == "general" and cat_tags:
                entry.category = _ct.primary_category(stripped)

            entry.sentiment = self.sentiment.analyze(stripped)

            self.memories.append(entry)
            self._hashes.add(entry.hash)
            self._content_norms.add(_cn)  # _cn computed above before dup checks
            self.search_engine.mark_dirty()

            if self.use_indexing and self.index_manager:
                self.index_manager.add_memory(entry)

            # WAL
            self._wal.append(entry.to_dict())
            count += 1

        if count:
            if self._read_cache is not None:
                self._read_cache.invalidate()
            if self._wal.should_flush():
                self.flush()

        return count

    # -- analysis & synthesis --------------------------------------------

    def analyze(self, query: str, limit: int = 10) -> Dict:
        """Analyze memories related to a query."""
        memories = self.search(query, limit=limit)
        if not memories:
            return {"status": "no_results", "query": query}
        
        # Basic analysis (can be enhanced)
        categories = defaultdict(int)
        sentiment_scores = []
        date_range = []
        
        for memory in memories:
            if memory.category:
                categories[memory.category] += 1
            if memory.sentiment and 'compound' in memory.sentiment:
                sentiment_scores.append(memory.sentiment['compound'])
            date_range.append(memory.created[:10])
        
        return {
            "status": "success",
            "query": query,
            "total_memories": len(memories),
            "categories": dict(categories),
            "avg_sentiment": sum(sentiment_scores) / len(sentiment_scores) if sentiment_scores else 0,
            "date_range": [min(date_range), max(date_range)] if date_range else [],
            "sample_memories": [m.content[:100] for m in memories[:3]]
        }

    def synthesize_knowledge(self, topic: str, limit: int = 20) -> Optional[str]:
        """Generate knowledge synthesis using the KnowledgeSynthesizer."""
        memories = self.search(topic, limit=limit)
        if not memories:
            return None
        
        memory_texts = [m.content for m in memories]
        header = f"Knowledge synthesis for '{topic}' ({len(memory_texts)} related memories):"
        body = "\n".join(f"- {text}" for text in memory_texts)
        return f"{header}\n{body}"

    # -- context packets --------------------------------------------------

    def build_context_packet(
        self,
        task: str,
        tags: Optional[List[str]] = None,
        category: str = None,
        environment: Optional[Dict[str, str]] = None,
        instructions: Optional[List[str]] = None,
        max_memories: int = 15,
        max_tokens: int = 4000,
        min_relevance: float = 0.1,
        include_mistakes: bool = True,
        max_pitfalls: int = 5,
    ) -> "ContextPacket":
        """Build a context packet for sub-agent spawning.

        Searches the memory store for relevant context and packages it
        into a structured ContextPacket that can be injected into a
        sub-agent's prompt at spawn time.

        Solves the "cold spawn" problem: sub-agents start with zero
        context, leading to confident mistakes based on incomplete
        information. A context packet gives them relevant onboarding.

        Args:
            task: Description of the sub-agent's task (used as search query).
            tags: Optional tag filters to narrow search.
            category: Optional category filter.
            environment: Key-value pairs (e.g. {"venv": ".venv", "python": "3.11"}).
            instructions: Explicit constraints for the sub-agent.
            max_memories: Maximum memories to include (default 15).
            max_tokens: Token budget for rendered output (default 4000).
            min_relevance: Minimum relevance score (default 0.1).

        Returns:
            ContextPacket with .render(), .to_dict(), .trim() methods.

        Example::

            packet = mem.build_context_packet(
                task="Verify package installation in venv",
                environment={"venv": ".venv"},
                instructions=["Check the venv, not global pip"],
            )
            prompt = f"{packet.render()}\\n\\nYOUR TASK: verify installation"
        """
        builder = ContextPacketBuilder(self)
        return builder.build(
            task=task,
            tags=tags,
            category=category,
            environment=environment,
            instructions=instructions,
            max_memories=max_memories,
            max_tokens=max_tokens,
            min_relevance=min_relevance,
            include_mistakes=include_mistakes,
            max_pitfalls=max_pitfalls,
        )

    def build_context_packet_multi(
        self,
        task: str,
        queries: List[str],
        environment: Optional[Dict[str, str]] = None,
        instructions: Optional[List[str]] = None,
        max_memories: int = 15,
        max_tokens: int = 4000,
        min_relevance: float = 0.1,
    ) -> "ContextPacket":
        """Build a context packet from multiple search queries.

        Useful when a task spans multiple topics. Deduplicates results
        across queries and merges into a single packet.

        Args:
            task: Overall task description.
            queries: List of search queries to run.
            environment: Optional environment context.
            instructions: Optional constraints.
            max_memories: Max total memories.
            max_tokens: Token budget.
            min_relevance: Minimum relevance threshold.

        Returns:
            Merged ContextPacket with deduplicated results.
        """
        builder = ContextPacketBuilder(self)
        return builder.build_multi(
            task=task,
            queries=queries,
            environment=environment,
            instructions=instructions,
            max_memories=max_memories,
            max_tokens=max_tokens,
            min_relevance=min_relevance,
        )

    # -- utility methods -------------------------------------------------

    # -- access pattern learning ------------------------------

    def _record_access(self, entry_id: str) -> None:
        """Increment the access count for a memory entry.

        Called automatically by ``search()`` for every returned result.
        Persisted to ``access_counts.json`` on the next ``flush()``.
        """
        self._access_tracker.record_access(entry_id)

    def get_hot_entries(self, top_n: int = 10) -> List[MemoryEntry]:
        """Return the *top_n* most frequently accessed memory entries.

        Useful for pre-loading important context into packets.

        Args:
            top_n: Number of hot entries to return.

        Returns:
            List of MemoryEntry objects, ordered by access count descending.
        """
        top_ids = self._access_tracker.get_top(top_n)
        hash_to_entry = {m.hash: m for m in self.memories}
        result = []
        for entry_id, _count in top_ids:
            if entry_id in hash_to_entry:
                result.append(hash_to_entry[entry_id])
        return result

    def _extract_tags(self, content: str) -> List[str]:
        """Extract tags from content."""
        tags = []
        content_lower = content.lower()
        
        # Extract explicit tags (@tag format)
        explicit_tags = re.findall(r'@([a-zA-Z][a-zA-Z0-9_-]*)', content)
        tags.extend(explicit_tags)
        
        # Auto-tag based on terms
        for term in self._tag_terms:
            if term.lower() in content_lower:
                tags.append(term)
        
        return list(set(tags))  # Remove duplicates

    def compact(self) -> Dict:
        """Compact memory storage: remove duplicates, expire stale entries, save.

        returns a richer result dict with shard counts and timing.

        Returns::

            {
                "shards_before": int,
                "shards_after": int,
                "entries_before": int,
                "entries_after": int,
                "space_freed_mb": float,
                "duration_ms": float,
                # backward-compat keys still present:
                "original_count": int,
                "final_count": int,
                "removed_count": int,
            }
        """
        _t0 = time.monotonic()

        # Snapshot shard count and disk usage before compaction
        shards_before = 0
        disk_before = 0.0
        if self.use_sharding and self.shard_manager:
            shards_dir = os.path.join(self.workspace, "shards")
            if os.path.isdir(shards_dir):
                shards_before = len([
                    f for f in os.listdir(shards_dir) if f.endswith(".json")
                ])
                disk_before = sum(
                    os.path.getsize(os.path.join(shards_dir, f))
                    for f in os.listdir(shards_dir) if f.endswith(".json")
                ) / (1024 * 1024)

        entries_before = len(self.memories)

        # Remove exact duplicates (by hash)
        seen_hashes: set = set()
        unique_memories = []
        for memory in self.memories:
            if memory.hash not in seen_hashes:
                unique_memories.append(memory)
                seen_hashes.add(memory.hash)

        # Apply decay-based forgetting: drop entries whose decay score is 0
        # (effectively dead entries that will never be recalled)
        # also retain entries with an enriched_summary (even if decay==0)
        # so that LLM-enriched entries are never silently dropped by compaction.
        retained_memories = [
            m for m in unique_memories
            if self.decay.score(m) > 0.0 or bool(getattr(m, 'enriched_summary', None))
        ]

        # Remove exact duplicates (identical normalized content), keeping the first
        # occurrence. Bloat prevention.
        dup_pairs = self.consolidation.find_duplicates(retained_memories)
        hashes_to_drop = {dup.hash for _first, dup in dup_pairs}
        consolidated_memories = [
            m for m in retained_memories if m.hash not in hashes_to_drop
        ]

        self.memories = consolidated_memories
        self._rebuild_content_indexes()
        self.search_engine.mark_dirty()

        # Invalidate cache - data has changed
        if self._read_cache is not None:
            self._read_cache.invalidate()

        # Rebuild indexes after compaction
        if self.use_indexing and self.index_manager:
            self.index_manager.rebuild_indexes(self.memories)

        # Flush compacted state to disk first, then rewrite the WAL to keep only
        # entries that were appended after the compaction snapshot began.
        pending_entries = self._wal.load_pending()
        pending_hashes = {entry.get("hash") for entry in pending_entries if entry.get("hash")}
        self.save()
        remaining_entries = []
        for entry in self._wal.load_pending():
            entry_hash = entry.get("hash")
            if entry_hash and entry_hash in pending_hashes:
                continue
            remaining_entries.append(entry)
        if remaining_entries:
            self._wal.replace_with_entries(remaining_entries)
        else:
            self._wal.clear()
        self._perf.record_compaction()

        # Snapshot shard count and disk usage after compaction
        shards_after = 0
        disk_after = 0.0
        if self.use_sharding and self.shard_manager:
            shards_dir = os.path.join(self.workspace, "shards")
            if os.path.isdir(shards_dir):
                shards_after = len([
                    f for f in os.listdir(shards_dir) if f.endswith(".json")
                ])
                disk_after = sum(
                    os.path.getsize(os.path.join(shards_dir, f))
                    for f in os.listdir(shards_dir) if f.endswith(".json")
                ) / (1024 * 1024)

        entries_after = len(self.memories)
        duration_ms = round((time.monotonic() - _t0) * 1000, 1)

        return {
            # spec keys
            "shards_before": shards_before,
            "shards_after": shards_after,
            "entries_before": entries_before,
            "entries_after": entries_after,
            "space_freed_mb": round(max(disk_before - disk_after, 0.0), 3),
            "duration_ms": duration_ms,
            # Backward-compatible keys
            "original_count": entries_before,
            "final_count": entries_after,
            "removed_count": entries_before - entries_after,
        }

    # -- system management -----------------------------------------------

    def get_stats(self) -> Dict:
        """Get comprehensive system statistics."""
        from . import __version__ as _pkg_version  # Bug fix: add package version to stats
        stats = {
            "schema_version": "0.4.0",    # storage format version (used by migration logic)
            "package_version": _pkg_version,  # parsica-spirit package version
            "total_memories": len(self.memories),
            "workspace": self.workspace,
            "features": {
                "sharding": self.use_sharding,
                "indexing": self.use_indexing
            }
        }
        
        if self.use_sharding and self.shard_manager:
            stats["sharding"] = self.shard_manager.index.get_stats()
        
        if self.use_indexing and self.index_manager:
            stats["indexing"] = self.index_manager.get_combined_stats()

        # Memory distribution by category
        categories = defaultdict(int)
        for memory in self.memories:
            categories[memory.category or "uncategorized"] += 1
        stats["categories"] = dict(categories)
        
        return stats

    def migrate_schema(self) -> Dict:
        """Manually trigger migration to v0.4 format."""
        return self.migration_manager.migrate("0.4.0")
    
    def rollback_migration(self) -> Dict:
        """Rollback the most recent migration."""
        return self.migration_manager.rollback()
    
    def validate_data(self) -> Dict:
        """Validate the current data schema."""
        return self.migration_manager.validate_schema()
    
    def rebuild_indexes(self):
        """Rebuild all search indexes from current memories."""
        if self.use_indexing and self.index_manager:
            self.index_manager.rebuild_indexes(self.memories)
            self.index_manager.save_all_indexes()
    
    def get_migration_history(self) -> List[Dict]:
        """Get history of applied migrations."""
        return self.migration_manager.get_migration_history()

    # -- Backward-compatible methods from v0.3 ----------------------

    def stats(self) -> Dict:
        """System statistics including performance metrics.

        Backward-compatible - all v0.3/v0.4 keys are preserved.  New
        keys are added under their own names.

        Returns::

            {
                # v0.3 compat keys
                "total": int,
                "avg_score": float,
                "archive_candidates": int,
                "sentiments": dict,
                "avg_confidence": float,
                "categories": dict,
                # performance keys
                "total_entries": int,
                "total_shards": int,
                "search_count": int,
                "avg_search_time_ms": float,
                "cache_hit_rate": float,
                "wal_pending_entries": int,
                "last_compaction": str | None,
                "disk_usage_mb": float,
                "memory_usage_mb": float,
            }
        """
        s = self.get_stats()
        now = datetime.now(timezone.utc)
        scores = [self.decay.score(m, now) for m in self.memories]
        sentiments: Dict[str, int] = defaultdict(int)
        for m in self.memories:
            dom = self.sentiment.dominant(m.sentiment)
            if dom:
                sentiments[dom] += 1
        # v0.3 compat
        s["total"] = len(self.memories)
        s["avg_score"] = round(sum(scores) / max(len(scores), 1), 4)
        s["archive_candidates"] = sum(1 for sc in scores if sc < self.decay.archive_threshold)
        s["sentiments"] = dict(sentiments)
        s["avg_confidence"] = round(
            sum(m.confidence for m in self.memories) / max(len(self.memories), 1), 4
        )

        # performance metrics
        shards_count = 0
        disk_mb = 0.0
        if self.use_sharding and self.shard_manager:
            shards_dir = os.path.join(self.workspace, "shards")
            if os.path.isdir(shards_dir):
                shard_files = [
                    os.path.join(shards_dir, f)
                    for f in os.listdir(shards_dir) if f.endswith(".json")
                ]
                shards_count = len(shard_files)
                disk_mb = sum(os.path.getsize(p) for p in shard_files) / (1024 * 1024)

        import sys
        mem_mb = sys.getsizeof(self.memories) / (1024 * 1024)

        s["total_entries"] = len(self.memories)
        s["total_shards"] = shards_count
        s["search_count"] = self._perf.search_count
        s["avg_search_time_ms"] = self._perf.avg_search_time_ms
        s["cache_hit_rate"] = (
            round(self._read_cache.hit_rate, 4) if self._read_cache is not None else 0.0
        )
        s["wal_pending_entries"] = self._wal.pending_count()
        s["last_compaction"] = self._perf.last_compaction
        s["disk_usage_mb"] = round(disk_mb, 3)
        s["memory_usage_mb"] = round(mem_mb, 3)
        s["enrichment_count"] = self._enrichment_count

        # additions
        try:
            import parsica_spirit.engine as _am
            s["version"] = getattr(_am, "__version__", "unknown")
        except Exception:
            s["version"] = "unknown"
        s["hot_entries"] = len(self.memories)
        s["warm_entries"] = len(getattr(self, '_warm_shard_names', []))
        s["cold_entries"] = len(getattr(self, '_cold_shard_names', []))
        s["wal_size"] = s["wal_pending_entries"]
        s["workspace"] = self.workspace
        s["agent_name"] = self.agent_name
        try:
            s["cooccurrence_pairs"] = len(self._cooccurrence._index) if hasattr(
                self._cooccurrence, '_index') else 0
        except Exception:
            s["cooccurrence_pairs"] = 0
        try:
            s["cache_size"] = len(self._read_cache._cache) if (
                self._read_cache and hasattr(self._read_cache, '_cache')) else 0
        except Exception:
            s["cache_size"] = 0
        try:
            import json as _json
            cost_path = os.path.join(self.workspace, "memory", "enrichment_costs.json")
            if os.path.exists(cost_path):
                with open(cost_path) as _f:
                    _cost = _json.load(_f)
                s["enrichment_cost_usd"] = round(_cost.get("lifetime_usd", 0.0), 6)
            else:
                s["enrichment_cost_usd"] = round(self._enrichment_count * 0.00005, 6)
        except Exception:
            s["enrichment_cost_usd"] = 0.0
        try:
            _gstats = self.get_graph_stats()
            s["graph_enabled"] = _gstats.get("graph_enabled", False)
            s["graph_nodes"] = _gstats.get("node_count", 0)
            s["graph_edges"] = _gstats.get("edge_count", 0)
        except Exception:
            s["graph_enabled"] = False
            s["graph_nodes"] = 0
            s["graph_edges"] = 0

        return s

    def get_enrichment_count(self, reset: bool = False) -> int:
        """Return the number of ENGINE-SIDE enrichments performed since last reset.

        Used by the plugin to track cumulative enrichment costs.

        Counts only enrichments produced by the engine's OWN constructor-injected
        enricher (ingest / re_enrich). Enrichments injected through the facade
        (Context.store -> EngineBackend.store with a pre-built record) are paid
        for and metered by the facade, not here, so this stays 0 on a
        facade-driven engine that was given no enricher of its own.

        Args:
            reset: If True, resets the counter to 0 after reading.

        Returns:
            Count of engine-side enrichments since last reset (or since startup).
        """
        count = self._enrichment_count
        if reset:
            self._enrichment_count = 0
        return count

    def ingest_with_gating(self, content: str, source: str = "inline",
                           context: Dict = None) -> int:
        """Ingest with P0-P3 input classification. P3 content is dropped.
        
        Multi-line content is split and each line classified independently.
        """
        # Metadata scrub gate: scrub the source label before it flows into the
        # gating context and into ingest() (ingest() re-scrubs it idempotently).
        source = self._scrub_str(source)
        count = 0
        for i, line in enumerate(content.split("\n")):
            stripped = line.strip()
            # Bug fix: align minimum length with ingest() (was 5, now 15)
            if len(stripped) < 15:
                continue
            
            # Scrub gate: redact secrets and PII in-place before storing
            # (parity with the Context facade - the engine never stores a raw secret or PII).
            stripped, _ = scrub(stripped, scrub_pii=True)
            gate_context = context or {}
            gate_context.update({"source": source, "line": i + 1})
            routing = self.gating.route(stripped, gate_context)
            if not routing["store"]:
                continue
            category = routing.get("category", "tactical")
            count += self.ingest(stripped, source=source, category=category)
        return count

    def on_date(self, date: str) -> List[MemoryEntry]:
        """Get memories from a specific date."""
        return self.temporal.on_date(self.memories, date)

    def between(self, start: str, end: str) -> List[MemoryEntry]:
        """Get memories between two dates."""
        return self.temporal.between(self.memories, start, end)

    def narrative(self, topic: str = None) -> str:
        """Build a narrative from memories, optionally filtered by topic."""
        return self.temporal.narrative(self.memories, topic=topic)

    def forget(self, topic: str = None, entity: str = None,
               before_date: str = None,
               session_id: Optional[str] = None) -> Dict:
        """Selectively forget memories with audit trail.

        ForgettingEngine has no ``forget()`` method; instead
        it exposes ``forget_topic()``, ``forget_entity()``, and
        ``forget_before()``.  This method now delegates correctly and supports
        combining multiple criteria (applied in sequence).

        Args:
            topic: Remove all memories containing this topic string.
            entity: Remove all memories mentioning this entity.
            before_date: Remove all memories created before this date (YYYY-MM-DD).

        Returns:
            Dict with keys ``removed`` (list[MemoryEntry]) and ``audit`` (dict).
        """
        # Session isolation - forget is a write (it mutates the store).
        if not self._check_write_gate(session_id, "forget"):
            return {"removed": [], "audit": self.forgetting.audit_log([])}

        if topic is None and entity is None and before_date is None:
            return {"removed": [], "audit": self.forgetting.audit_log([])}

        current = list(self.memories)
        all_forgotten: List = []

        if topic is not None:
            current, forgotten = self.forgetting.forget_topic(current, topic)
            all_forgotten.extend(forgotten)

        if entity is not None:
            current, forgotten = self.forgetting.forget_entity(current, entity)
            all_forgotten.extend(forgotten)

        if before_date is not None:
            current, forgotten = self.forgetting.forget_before(current, before_date)
            all_forgotten.extend(forgotten)

        # Deduplicate (a memory might match multiple criteria)
        seen: set = set()
        unique_forgotten = []
        for m in all_forgotten:
            if m.hash not in seen:
                unique_forgotten.append(m)
                seen.add(m.hash)

        result = {
            "removed": unique_forgotten,
            "audit": self.forgetting.audit_log(unique_forgotten),
        }

        if unique_forgotten:
            removed_set = {m.hash for m in unique_forgotten}
            # Bug fix: `current` is already the post-forget filtered list from
            # forget_topic/forget_entity/forget_before - no need to re-filter.
            self.memories = current
            # rebuild all dedup indexes so re-ingesting the same content
            # after a forget() is not blocked by stale entries.
            self._rebuild_content_indexes()
            self.search_engine.mark_dirty()
            if self._read_cache is not None:
                self._read_cache.invalidate()

            # purge forgotten entries from the WAL so they cannot
            # resurrect via _replay_wal() on next load.  Rewrite the WAL
            # keeping only entries whose hashes are NOT in the removed set.
            pending = self._wal.load_pending()
            surviving = [e for e in pending if e.get("hash") not in removed_set]
            if len(surviving) < len(pending):
                if surviving:
                    self._wal.replace_with_entries(surviving)
                else:
                    self._wal.clear()

            # Also purge from the index WAL
            try:
                idx_reader = WALReader()
                idx_reader.open(self._index_wal_path)
                idx_entries = idx_reader.read_all()
                if idx_entries:
                    idx_surviving = [e for e in idx_entries if e.get("hash") not in removed_set]
                    if len(idx_surviving) < len(idx_entries):
                        idx_reader.clear()
                        self._index_wal_writer.close()
                        self._index_wal_writer = WALWriter()
                        self._index_wal_writer.open(self._index_wal_path)
                        for entry in idx_surviving:
                            self._index_wal_writer.append(entry)
                        self._index_wal_entry_count = len(idx_surviving)
            except Exception:
                logger.debug("Index WAL purge failed after forget; will rebuild on restart", exc_info=True)

            # Log to audit (before save so the trail is recorded even if
            # save hits an I/O error - audit is non-critical, durability is).
            for m in unique_forgotten:
                self._append_audit({"action": "forget", "hash": m.hash,
                                    "content_preview": m.content[:50],
                                    "timestamp": datetime.now(timezone.utc).isoformat()})

            # Persist current truth to shard/legacy files.
            # Without this, time-shard mode would still have deleted entries
            # in the weekly JSONL files, causing resurrection on reload.
            self.save()

        return result

    def reindex(self) -> None:
        """Force a full search index rebuild."""
        self.search_engine.reindex(self.memories)

    def consolidate(self) -> Dict:
        """Run consolidation: dedup, clustering, contradiction detection."""
        return self.consolidation.run(self.memories)

    def compress_old(self, days: int = 7) -> list:
        """Compress memories older than N days."""
        mem_dir = os.path.join(self.workspace, "memory")
        return self.compression.compress_old_files(mem_dir, days)

    def _register_entry(self, entry: "MemoryEntry") -> bool:
        """The single sanitized commit path for a producer that builds a whole MemoryEntry rather
        than ingesting lines (synthesis today). It closes the gap GPT's review found: synthesized
        research text is fresh EXTERNAL input, and appending it directly bypassed the scrub AND the
        engine bookkeeping, so a secret rode onto disk and the same content re-ingested as a
        non-duplicate. Here the content + provenance metadata are scrubbed, the id is recomputed from
        the CLEAN content (never a restored hash), the entry is deduped, and every index ingest
        maintains is updated. Returns True if committed, False if scrubbed-empty or a duplicate."""
        clean, _ = scrub(entry.content or "", scrub_pii=True)
        clean = clean.strip()
        if not clean:
            return False
        entry.content = clean
        entry.recompute_hash()                       # id from the CLEAN content, never a restored hash
        entry.source = self._scrub_str(entry.source)
        entry.category = self._scrub_str(entry.category)
        entry.tags = self._scrub_tag_list(entry.tags)
        if entry.enriched_summary:
            entry.enriched_summary = self._scrub_str(entry.enriched_summary)

        if entry.hash in self._hashes:
            return False
        _cn = _TURN_PREFIX_RE.sub('', clean).strip().lower()
        if _cn in self._content_norms:
            return False

        self.memories.append(entry)
        self._hashes.add(entry.hash)
        self._content_norms.add(_cn)
        self.search_engine.mark_dirty()
        if self.use_indexing and self.index_manager:
            self.index_manager.add_memory(entry)
        self._wal.append(entry.to_dict())
        if self._graph_enabled and self._graph is not None:
            try:
                self._graph.index_entry(entry.hash, entry.content)
            except Exception:
                logger.debug("Graph index_entry failed for %s", entry.hash[:12], exc_info=True)
        if not self.use_sharding and self._time_window_sharding and self._time_window_shards:
            self._time_window_shards.write_entry(entry)
        try:
            self._index_wal_writer.append(entry.to_dict())
            self._index_wal_entry_count += 1
        except Exception:
            logger.debug("Index WAL append failed; will rebuild on restart", exc_info=True)
        self._update_cooccurrence(clean)
        if self._read_cache is not None:
            self._read_cache.invalidate()
        return True

    def synthesize(self, research_results: Dict = None,
                   session_id: Optional[str] = None) -> Dict:
        """Integrate research findings into memory."""
        # Session isolation - synthesis writes new entries into the store.
        if not self._check_write_gate(session_id, "synthesize"):
            return {"synthesized_entries": 0, "new_entries": []}
        report = self.synthesis.run_cycle(self.memories, research_results=research_results)
        # Route every synthesized entry through the single sanitized commit path: synthesized
        # content is fresh external research text, so it must pass the SAME scrub + dedup + index
        # bookkeeping as ingest rather than being appended raw (GPT review S1).
        if report.get("synthesized_entries", 0) > 0 and "new_entries" in report:
            committed = 0
            for entry_dict in report["new_entries"]:
                entry = MemoryEntry.from_dict(entry_dict)
                if "synthesis" not in entry.tags:
                    entry.tags.append("synthesis")
                if self._register_entry(entry):
                    committed += 1
            report["synthesized_entries"] = committed   # report what actually committed post-scrub/dedup
        return report

    def research_suggestions(self, limit: int = 5) -> List[Dict]:
        """Get suggestions for knowledge gaps."""
        return self.synthesis.suggest_research_topics(self.memories, limit=limit)

    # -- Export / Import API ---------------------------------

    def export(self, output_path: str, include_metadata: bool = True) -> int:
        """Export all memories to a JSON file.

        Args:
            output_path: Destination file path.
            include_metadata: When True (default), includes full metadata
                (workspace path, export timestamp, version tag).  When False,
                only the ``entries`` list is included (useful for data portability).

        Returns:
            Count of exported entries.

        Format::

            {
                "version": "4.2",
                "exported_at": "<ISO 8601>",
                "workspace": "<path>",
                "entries": [
                    {
                        "content": str,
                        "source": str,
                        "category": str,
                        "memory_type": str,
                        "timestamp": str,
                        "score": float
                    },
                    ...
                ]
            }
        """
        now = datetime.now(timezone.utc)
        entries = []
        for m in self.memories:
            entry_dict = {
                "content": m.content,
                "source": getattr(m, "source", ""),
                "category": m.category or "general",
                "memory_type": getattr(m, "memory_type", "episodic"),
                "timestamp": m.created,
                "score": self.decay.score(m, now),
            }
            entries.append(entry_dict)

        # Always augment entries with hash so import_from() can deduplicate correctly
        for m, e in zip(self.memories, entries):
            e["_hash"] = m.hash

        if include_metadata:
            data: dict = {
                "version": "4.2",
                "exported_at": now.isoformat(),
                "workspace": self.workspace,
                "entries": entries,
            }
        else:
            data = {"entries": entries}

        atomic_write_json(output_path, data)
        return len(entries)

    def import_from(self, input_path: str, merge: bool = True) -> int:
        """Import memories from a JSON file exported by :meth:`export`.

        Args:
            input_path: Path to the JSON file produced by ``export()``.
            merge: When ``True`` (default) imported entries are *added* to the
                existing store - duplicates (same content hash) are skipped.
                When ``False`` the existing store is **replaced** with the
                imported entries.

        Returns:
            Count of imported entries (after deduplication when merge=True).

        Ingress + crash safety (S1): the import file is UNTRUSTED external data. Each entry's
        content/source/category is scrubbed and its memory_type validated before it can reach
        the store, and the entry's hash is RECOMPUTED from the sanitised content (the file's
        ``_hash`` is not trusted - a scrub changes the content, so a stored hash would be wrong
        anyway, and a tampered one could poison dedup). The whole file is staged first and only
        then swapped in (never clear-then-fill): a malformed file or a mid-loop error can leave
        the store neither wiped nor half-imported. In replace mode a file with zero valid entries
        does NOT wipe the store - a deliberate clear uses the explicit reset API, never a
        side effect of a bad import (no silent data destruction).
        """
        with open(input_path, encoding="utf-8") as fh:
            raw = json.load(fh)

        # Support both the versioned envelope {"entries": [...]} and a raw list
        if isinstance(raw, dict):
            entries_data = raw.get("entries", [])
        elif isinstance(raw, list):
            entries_data = raw
        else:
            return 0
        if not isinstance(entries_data, list):
            return 0

        # STAGE every sanitised entry BEFORE touching the live store (transactional:
        # stage-then-swap). Only allowlisted fields are read - any other key in a hostile
        # entry_dict (e.g. a secret in type_metadata) is simply never consulted.
        staged: List[MemoryEntry] = []
        staged_hashes: set = set()
        for entry_dict in entries_data:
            if not isinstance(entry_dict, dict):
                continue
            try:
                content, _ = scrub(entry_dict.get("content", ""), scrub_pii=True)
                source = self._scrub_str(entry_dict.get("source", "import"))
                category = self._scrub_str(entry_dict.get("category", "general"))
                # route the type through the ONE ingress gateway (scrub-collapse + normalise),
                # not a bare normalize_memory_type, so import cannot persist a credential-shaped slug.
                memory_type, _ = self._accept_ingress(entry_dict.get("memory_type", DEFAULT_TYPE), None)

                # MemoryEntry recomputes hash from the SANITISED source/line/content - the
                # export file's _hash is deliberately NOT restored (untrusted, and stale
                # after a scrub). Dedup below runs on this recomputed hash.
                entry = MemoryEntry(content, source, 0, category, memory_type=memory_type)

                # Restore timestamps only when they PARSE as ISO-8601 - a credential/PII-shaped
                # string in the timestamp field must never persist raw (it would ride into
                # created/last_accessed, the index files, and the shard filename). An invalid
                # value leaves MemoryEntry's fresh now() stamps in place.
                ts = _iso_ts_or_none(entry_dict.get("timestamp"))
                if ts is not None:
                    entry.created = ts
                    entry.last_accessed = ts
            except Exception:
                continue  # skip a malformed entry; the rest of the import still stages

            if entry.hash in staged_hashes:
                continue  # dedup within the incoming batch
            staged.append(entry)
            staged_hashes.add(entry.hash)

        # SWAP - only now mutate the live store, after staging fully succeeded.
        count = 0
        if merge:
            for entry in staged:
                # Dedup on the recomputed hash OR the content-norm. The content-norm check
                # matters because the hash is recomputed (line reset to 0), so re-importing an
                # export back into its own store would otherwise duplicate every entry - the
                # content-norm catches that without trusting the file's stale/tampered hash.
                _cn = _TURN_PREFIX_RE.sub('', entry.content).strip().lower()
                if entry.hash in self._hashes or _cn in self._content_norms:
                    continue
                self.memories.append(entry)
                self._hashes.add(entry.hash)
                self._content_norms.add(_cn)
                count += 1
        else:
            # Replace mode: NEVER wipe on an empty/all-invalid import - preserve the store.
            if not staged:
                return 0
            self.memories = list(staged)
            self._rebuild_content_indexes()   # rebuilds _hashes / _content_norms from the swap
            count = len(staged)

        if count:
            self.search_engine.mark_dirty()
            if self._read_cache is not None:
                self._read_cache.invalidate()

        return count

    def import_memories(self, path: str) -> int:
        """Backward-compat alias for :meth:`import_from` with merge=True.

        Args:
            path: Path to the JSON array file produced by ``export()``.

        Returns:
            Number of new entries added.
        """
        return self.import_from(path, merge=True)

    def _append_audit(self, entry: Dict) -> None:
        """Append an entry to the audit log."""
        audit = []
        if os.path.exists(self.audit_path):
            with open(self.audit_path) as f:
                audit = json.load(f)
        audit.append(entry)
        atomic_write_json(self.audit_path, audit)

    # -- Instrumentation --------------------------------------------------

    def mark_retrieved(self, memory_ids: List[str], context) -> None:
        """Record that a set of memories were retrieved for a query.

        Called automatically when instrumentation_context is passed to search().
        Logs the retrieval event to the instrumentation log for later analysis.

        Args:
            memory_ids: List of memory hashes that were retrieved.
            context: SearchContext object for this search request.
        """
        context.retrieved_memory_ids = list(memory_ids)
        event = {
            "event": "retrieved",
            "request_id": context.request_id,
            "memory_ids": memory_ids,
            "count": len(memory_ids),
            "timestamp": context.timestamp,
            "context_id": context.context_id,
        }
        # Privacy: raw query text is never persisted.
        try:
            with open(self._instrumentation_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")
        except OSError:
            pass  # Instrumentation failures are non-fatal

    def mark_used(self, memory_ids: List[str], context=None) -> int:
        """Signal that specific memories were used in the agent's response.

        Boosts the relevance of used memories and resets their decay age.

        Args:
            memory_ids: List of memory hashes that appeared in the response.
            context: Optional SearchContext for linking to the retrieval event.

        Returns:
            Number of memories successfully boosted.
        """
        if context is not None:
            context.used_memory_ids = list(memory_ids)

        boosted = 0
        for memory_id in memory_ids:
            if self.boost_relevance(memory_id):
                boosted += 1

        # Log usage event
        event = {
            "event": "used",
            "memory_ids": memory_ids,
            "count": len(memory_ids),
            "timestamp": time.time(),
            "request_id": context.request_id if context else None,
        }
        try:
            with open(self._instrumentation_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event) + "\n")
        except OSError:
            pass

        # Log to audit logger if available
        if context is not None and hasattr(context, 'audit_logger') and context.audit_logger is not None:
            try:
                for memory_id in memory_ids:
                    context.audit_logger.log("recall", {
                        "memory_id": memory_id,
                        "context_id": context.context_id,
                        "request_id": context.request_id,
                        "session_id": context.session_id,
                    })
            except Exception:
                # Audit logging failures are non-fatal
                pass

        # Invalidate cache so boosted memories rank higher on next search
        if boosted and self._read_cache is not None:
            self._read_cache.invalidate()

        return boosted

    def boost_relevance(self, memory_id: str, multiplier: float = 1.5) -> bool:
        """Boost the importance/relevance of a memory by ID.

        Increases the memory's importance score and resets its decay age,
        making it surface higher in future searches.

        Args:
            memory_id: The memory hash to boost.
            multiplier: Importance multiplier (default 1.5 = 50% boost).

        Returns:
            True if the memory was found and boosted, False otherwise.
        """
        for entry in self.memories:
            if entry.hash == memory_id:
                entry.importance = min(entry.importance * multiplier, 10.0)
                # Reset decay: freshen the memory as if it was just accessed
                entry.last_accessed = datetime.now(timezone.utc).isoformat()
                entry.access_count += 1
                self.search_engine.mark_dirty()
                return True
        return False

    # -- Co-occurrence Semantic Boost --------------------------------------

    def search_with_context(
        self,
        query: str,
        limit: int = 20,
        instrumentation_context=None,
        cooccurrence_boost: bool = True,
        **kwargs,
    ) -> tuple:
        """Search memories with optional instrumentation and co-occurrence boost.

        Returns (results, SearchContext) for use with mark_used().

        Args:
            query: Search query string.
            limit: Maximum results to return.
            instrumentation_context: Optional pre-built SearchContext.
                If None, one is created automatically.
            cooccurrence_boost: Whether to apply local co-occurrence re-ranking.
            **kwargs: Additional args forwarded to search() (explain, category, etc.)

        Returns:
            Tuple of (results, SearchContext).
        """
        from .instrumentation import SearchContext

        ctx = instrumentation_context or SearchContext(query=query)

        # Run BM25 search (fetch extra for re-ranking)
        fetch_limit = limit * 2 if cooccurrence_boost else limit
        results = self.search(query, limit=fetch_limit, explain=True, **kwargs)

        # Co-occurrence re-ranking
        if cooccurrence_boost and self._cooccurrence.vocab_size > 0:
            for r in results:
                boost = self._cooccurrence.boost_score(query, r.entry.content)
                r.score += boost

            results.sort(key=lambda r: r.score, reverse=True)

        results = results[:limit]

        # Record retrieval for instrumentation
        retrieved_ids = [r.entry.hash for r in results]
        self.mark_retrieved(retrieved_ids, ctx)

        # Return plain entries by default (backward compat), explain if needed
        if kwargs.get("explain"):
            return results, ctx

        entries = []
        for r in results:
            entry_copy = copy.copy(r.entry)
            entry_copy.confidence = r.relevance
            entries.append(entry_copy)
        return entries, ctx

    def _update_cooccurrence(self, content: str) -> None:
        """Update the local co-occurrence index when a memory is stored.

        Note: does NOT save to disk on every call - deferred to flush() to
        avoid O(n) disk writes during bulk ingestion (e.g. ingest_directory).
        """
        self._cooccurrence.update_from_memory(content)

    def _feed_cooccurrence_from_enrichment(self, original: str, enrichment: dict) -> None:
        """Feed enrichment-derived semantic relationships into the co-occurrence index.

        Constructs a virtual sentence from enrichment tags + keywords + original
        terms, then passes it through update_from_memory so all semantic pairs
        are captured. This bootstraps the co-occurrence matrix with high-quality
        LLM-derived relationships even before natural conversational data accumulates.

        runs on every enriched ingest, non-fatal.
        """
        try:
            parts = []
            # Add original content tokens
            parts.append(original.lower())
            # Add enrichment tags (normalised to words)
            for tag in enrichment.get("tags", []):
                parts.append(tag.lower().replace("_", " "))
            # Add enrichment keywords
            for kw in enrichment.get("keywords", []):
                parts.append(str(kw).lower().replace("_", " "))
            # Add enriched summary if present
            if enrichment.get("summary"):
                parts.append(enrichment["summary"].lower())
            virtual_sentence = " ".join(parts)
            self._cooccurrence.update_from_memory(virtual_sentence)
        except Exception:
            pass  # Non-fatal

    def re_enrich(
        self,
        batch_size: int = 50,
        progress_fn=None,
        overwrite: bool = False,
        decompose: bool = False,
        session_id: Optional[str] = None,
    ) -> int:
        """Re-enrich all existing memories through the configured enricher.

        Processes entries in batch_size chunks. Skips already-enriched entries
        unless overwrite=True. Rebuilds the search index after completion and
        saves all shards.

        bootstraps the co-occurrence matrix with 90K+ word pairs
        when run against an existing ~9,000 entry memory store.

        decompose (opt-in, default False) also extracts atomic facts from the
        enrichment response and stores them as separate MemoryEntry objects
        (memory_type="fact", provenance="decomposed"). Applies the same hash
        and content-norm dedup checks as ingest(). Entries that are
        already facts themselves are never decomposed further.

        Args:
            batch_size: Progress callback fired every N enriched entries.
            progress_fn: Optional callable(enriched_count, total_count).
            overwrite: If True, re-enrich even entries that already have
                       an enriched_summary. Default False (idempotent).
            decompose: If True, extract and store atomic facts from the
                       enrichment response, mirroring the behaviour of ingest().
                       Default False - fact extraction is opt-in (skipping it is
                       faster and matches the original re_enrich behaviour).

        Returns:
            Count of entries enriched (fact entries created are not counted
            towards this total; use re_decompose() stats for that).
        """
        # Session isolation - re-enrichment writes back into the store.
        if not self._check_write_gate(session_id, "re_enrich"):
            return 0

        if self._enricher is None:
            return 0

        count = 0
        total = len(self.memories)
        # Snapshot to avoid iterating over newly-added fact entries
        snapshot = list(self.memories)

        for entry in snapshot:
            if not overwrite and getattr(entry, "enriched_summary", ""):
                continue  # Already enriched - skip unless overwrite requested

            try:
                enrichment = self._enricher(entry.content)
                if enrichment and isinstance(enrichment, dict):
                    # Scrub enricher output before copying fields / feeding
                    # co-occurrence / decomposing (external callable).
                    enrichment = self._scrub_enrichment_dict(enrichment)
                    if enrichment.get("tags"):
                        entry.tags = sorted(
                            set(entry.tags) | {t.lower() for t in enrichment["tags"]}
                        )
                    if enrichment.get("summary"):
                        entry.enriched_summary = str(enrichment["summary"])
                    if enrichment.get("keywords"):
                        entry.search_keywords = [
                            str(k).lower() for k in enrichment["keywords"]
                        ]
                    if enrichment.get("search_queries"):
                        entry.search_queries = [
                            str(q) for q in enrichment["search_queries"]
                        ]
                    self._feed_cooccurrence_from_enrichment(entry.content, enrichment)
                    self._enrichment_count += 1
                    count += 1

                    # Fact decomposition - mirror ingest() behaviour
                    if decompose and entry.memory_type != "fact":
                        self._decompose_facts(
                            enrichment, entry, entry.source, entry.category,
                            getattr(entry, "session_id", ""),
                            getattr(entry, "agent_id", self.agent_name),
                        )

            except Exception:
                pass  # Non-fatal - leave entry as-is

            if progress_fn and (count % batch_size == 0) and count > 0:
                progress_fn(count, total)

        if count:
            # Rebuild search index with enriched content
            self.search_engine.mark_dirty()
            # Save all shards with updated entries
            try:
                self.save()
            except Exception:
                pass
            # Save co-occurrence index
            try:
                self._cooccurrence.save()
            except Exception:
                pass

        return count


    def re_decompose(
        self,
        batch_size: int = 50,
        progress_fn=None,
        overwrite: bool = False,
        session_id: Optional[str] = None,
    ) -> int:
        """Extract atomic facts from existing memories and store as separate entries.

        Runs the enricher on each memory to extract facts, then creates
        new fact-type entries with provenance="decomposed" and parent_hash linking
        back to the source. Applies hash and content-norm dedup to avoid duplicate facts.

        Args:
            batch_size: Progress callback fired every N processed entries.
            progress_fn: Optional callable(processed_count, total_count).
            overwrite: If True, re-decompose even entries that already spawned facts.
                Default False (skips entries whose hash appears as a parent_hash).

        Returns:
            Count of new fact entries created.
        """
        # Session isolation - decomposition writes new fact entries.
        if not self._check_write_gate(session_id, "re_decompose"):
            return 0

        if self._enricher is None:
            return 0

        # Build set of parent hashes to skip already-decomposed entries
        existing_parents = set()
        if not overwrite:
            for m in self.memories:
                ph = getattr(m, "parent_hash", "")
                if ph:
                    existing_parents.add(ph)

        new_facts = 0
        processed = 0
        total = len(self.memories)
        # Snapshot the current memory list to avoid iterating over new additions
        snapshot = list(self.memories)

        for entry in snapshot:
            # Skip fact entries themselves (don't decompose facts into more facts)
            if entry.memory_type == "fact":
                continue
            # Skip already-decomposed entries unless overwrite
            if not overwrite and entry.hash in existing_parents:
                continue

            try:
                enrichment = self._enricher(entry.content)
                if not enrichment or not isinstance(enrichment, dict):
                    continue
                # Scrub enricher output before decomposing (external callable);
                # _decompose_facts also scrubs each fact as a final guarantee.
                enrichment = self._scrub_enrichment_dict(enrichment)

                new_facts += self._decompose_facts(
                    enrichment, entry, entry.source, entry.category,
                    getattr(entry, "session_id", ""),
                    getattr(entry, "agent_id", self.agent_name),
                )

            except Exception:
                pass  # Non-fatal

            processed += 1
            if progress_fn and (processed % batch_size == 0) and processed > 0:
                progress_fn(processed, total, new_facts)

        if new_facts:
            self.search_engine.mark_dirty()
            try:
                self.save()
            except Exception:
                pass

        return new_facts

    def delete_source(self, source_url: str) -> dict:
        """Remove all memories originating from a specific source URL.

        Args:
            source_url: The source_url to match against.

        Returns:
            {"deleted": N, "source_url": source_url}
        """
        try:
            from .ingest_manifest import IngestManifest

            to_remove = [m for m in self.memories
                         if getattr(m, "source_url", None) == source_url]
            removed_hashes = {m.hash for m in to_remove}

            self.memories = [m for m in self.memories if m.hash not in removed_hashes]
            # _rebuild_content_indexes() reconstructs _hashes and
            # _content_norms from self.memories - no need to manually
            # subtract removed_hashes first.
            self._rebuild_content_indexes()

            if to_remove:
                self.search_engine.mark_dirty()
                if self._read_cache is not None:
                    self._read_cache.invalidate()

                # purge deleted entries from WAL to prevent resurrection
                pending = self._wal.load_pending()
                surviving = [e for e in pending if e.get("hash") not in removed_hashes]
                if len(surviving) < len(pending):
                    if surviving:
                        self._wal.replace_with_entries(surviving)
                    else:
                        self._wal.clear()

                # Also purge deleted entries from the index WAL.
                # fix: previous code used string / operator (TypeError)
                # and WALWriter methods that don't exist. Now uses WALReader
                # (same pattern as forget()).
                try:
                    idx_reader = WALReader()
                    idx_reader.open(self._index_wal_path)
                    idx_entries = idx_reader.read_all()
                    if idx_entries:
                        idx_surviving = [e for e in idx_entries if e.get("hash") not in removed_hashes]
                        if len(idx_surviving) < len(idx_entries):
                            idx_reader.clear()
                            self._index_wal_writer.close()
                            self._index_wal_writer = WALWriter()
                            self._index_wal_writer.open(self._index_wal_path)
                            for entry in idx_surviving:
                                self._index_wal_writer.append(entry)
                            self._index_wal_entry_count = len(idx_surviving)
                except Exception:
                    logger.debug("Index WAL purge failed after delete_source; will rebuild on restart", exc_info=True)

                # Log to audit (before save so the trail is recorded even if
                # save hits an I/O error - audit is non-critical, durability is).
                for m in to_remove:
                    self._append_audit({"action": "delete_source", "hash": m.hash,
                                        "source_url": source_url,
                                        "content_preview": m.content[:50],
                                        "timestamp": datetime.now(timezone.utc).isoformat()})

                # Persist current truth to shard/legacy files.
                # Without this, time-shard mode would still have deleted entries
                # in the weekly JSONL files, causing resurrection on reload.
                self.save()

            # Clean manifest
            try:
                manifest = IngestManifest(self.workspace)
                manifest.remove(source_url)
                manifest.save()
            except Exception:
                pass

            return {"deleted": len(to_remove), "source_url": source_url}
        except Exception as e:
            import logging
            logging.getLogger("parsica_spirit.engine").error("delete_source failed: %s", e)
            return {"deleted": 0, "source_url": source_url, "error": str(e)}


    # -- Graph Intelligence public API --------------------------------

    def graph_search(
        self,
        subject: str,
        relation: str = None,
        obj: str = None,
    ) -> list:
        """
        Relationship-aware graph search.

        Examples:
          mem.graph_search("alice")                   -> all relationships involving alice
          mem.graph_search("alice", "created_by")     -> what did alice create?
          mem.graph_search("alice", obj="parsica")    -> how are alice and parsica related?

        Returns list of (source, relation, target, entry_ids) tuples.
        Returns empty list if graph is disabled or entity not found.
        """
        try:
            if not self._graph_enabled or self._graph is None:
                return []
            return self._graph.relationship_search(subject, relation, obj)
        except Exception:
            return []

    def entity_path(self, source: str, target: str, max_hops: int = 3) -> list:
        """
        Find the relationship path between two entities in the memory graph.

        Returns list of entity canonicals from source -> target (inclusive),
        or empty list if no path exists or graph is disabled.

        Example:
          mem.entity_path("alice", "parsica-spirit")
          -> ["alice", "created_by", "parsica-spirit"]  (if path exists)
        """
        try:
            if not self._graph_enabled or self._graph is None:
                return []
            result = self._graph.find_path(source, target, max_hops=max_hops)
            return result or []
        except Exception:
            return []

    def get_entity(self, canonical: str) -> dict:
        """
        Get information about a specific entity in the memory graph.

        Returns dict with entity info or empty dict if not found.
        """
        try:
            if not self._graph_enabled or self._graph is None:
                return {}
            node = self._graph.get_node(canonical)
            if node is None:
                return {}
            from dataclasses import asdict
            return asdict(node)
        except Exception:
            return {}

    def get_health(self) -> dict:
        """
        Simple readiness check for monitoring / health endpoints.

        Returns:
          {"status": "ok", "checks": {<name>: True/False, ...}}

        Status is "ok" only when all checks pass.
        Non-fatal: any check that throws is marked False.
        """
        checks = {}

        # Workspace is accessible
        try:
            checks["workspace_accessible"] = os.path.isdir(self.workspace)
        except Exception:
            checks["workspace_accessible"] = False

        # Memory store has entries loaded
        try:
            checks["memories_loaded"] = len(self.memories) >= 0  # always true, 0 is fine
        except Exception:
            checks["memories_loaded"] = False

        # WAL is functional
        try:
            checks["wal_ok"] = self._wal is not None
        except Exception:
            checks["wal_ok"] = False

        # Search engine is responsive
        try:
            self.search("__health_check__", limit=1)
            checks["search_ok"] = True
        except Exception:
            checks["search_ok"] = False

        # Graph (if enabled)
        try:
            if self._graph_enabled:
                checks["graph_ok"] = self._graph is not None
            else:
                checks["graph_ok"] = True  # disabled = not a failure
        except Exception:
            checks["graph_ok"] = False

        # Bootstrap file size check - some agent runtimes inject workspace files into
        # context with a hard ~35,000 char limit. Files over this limit are silently
        # truncated, causing agents to lose memory context without any visible error.
        bootstrap_warnings = None
        bootstrap_error = None
        try:
            bootstrap_warnings = self.check_bootstrap_files()
            checks["bootstrap_files_ok"] = len(bootstrap_warnings) == 0
            if bootstrap_warnings:
                import warnings
                for w in bootstrap_warnings:
                    warnings.warn(w, RuntimeWarning, stacklevel=2)
        except Exception as exc:
            # A check that could not run is NOT a pass. Until October 2026 this branch set the check
            # to True (REVIEW-2026-10.md, pile one, class (a), core.py 3606-3607).
            checks["bootstrap_files_ok"] = False
            bootstrap_error = type(exc).__name__

        status = "ok" if all(checks.values()) else "degraded"
        result = {"status": status, "checks": checks}

        # Surface bootstrap warnings (and a failed check) in the health report for easy visibility
        if bootstrap_warnings:
            result["bootstrap_warnings"] = bootstrap_warnings
        if bootstrap_error:
            result["bootstrap_check_error"] = bootstrap_error

        return result

    def check_bootstrap_files(
        self,
        workspace_dir: Optional[str] = None,
        warn_threshold: int = 30_000,
        hard_limit: int = 35_000,
    ) -> list:
        """
        Scan common agent bootstrap files and warn if they exceed the host runtime's
        ~35,000 char injection limit.

        Several agent runtimes inject workspace files like MEMORY.md,
        AGENTS.md, SOUL.md into each session context. Files over ~35K chars are
        silently truncated - agents lose the tail end of their memory bootstrap
        on every message without any visible error.

        Args:
            workspace_dir: Directory to scan. Defaults to parent of self.workspace.
            warn_threshold: Warn early at this char count (default 30,000).
            hard_limit: Hard truncation limit (default 35,000).

        Returns:
            List of warning strings. Empty list = all files OK.
        """
        warnings_out = []
        bootstrap_files = [
            "MEMORY.md",
            "AGENTS.md",
            "SOUL.md",
            "USER.md",
            "IDENTITY.md",
            "CONTEXT.md",
            "SYSTEM.md",
            "HEARTBEAT.md",
        ]

        try:
            # Try workspace parent first (standard layout: workspace/memory_store -> workspace/MEMORY.md)
            scan_dir = workspace_dir or os.path.dirname(self.workspace.rstrip("/"))
            if not os.path.isdir(scan_dir):
                scan_dir = self.workspace

            for fname in bootstrap_files:
                fpath = os.path.join(scan_dir, fname)
                if not os.path.isfile(fpath):
                    continue
                try:
                    size = os.path.getsize(fpath)
                    if size >= hard_limit:
                        warnings_out.append(
                            f"{fname} is {size:,} chars - EXCEEDS the runtime's {hard_limit:,} char "
                            f"bootstrap limit. Content is being silently truncated every session. "
                            f"Prune this file immediately to restore full memory injection. ({fpath})"
                        )
                    elif size >= warn_threshold:
                        warnings_out.append(
                            f"{fname} is {size:,} chars - approaching the runtime's {hard_limit:,} char "
                            f"bootstrap limit (warn at {warn_threshold:,}). "
                            f"Consider pruning before it causes silent truncation. ({fpath})"
                        )
                except OSError as exc:
                    # a file that cannot be inspected is reported, never silently counted as fine
                    warnings_out.append(
                        f"{fname} could not be inspected ({type(exc).__name__}); its size is unknown, "
                        f"not OK. ({fpath})"
                    )
        except Exception as exc:
            # Never raise - this is a diagnostic - but never report an unfinished scan as all clear
            # either (until October 2026 this returned an empty list, which get_health read as OK).
            warnings_out.append(
                f"bootstrap file check could not complete ({type(exc).__name__}); treat the result "
                f"as unknown, not OK."
            )

        return warnings_out

    def get_graph_stats(self) -> dict:
        """Return memory graph statistics (node count, edge count, top entities)."""
        try:
            if not self._graph_enabled or self._graph is None:
                return {"graph_enabled": False}
            return {**self._graph.get_stats(), "graph_enabled": True}
        except Exception:
            return {"graph_enabled": False}

    def rebuild_graph(self) -> int:
        """
        Rebuild the memory graph from scratch by re-indexing all loaded memories.

        Useful after bulk ingestion or when the graph file is stale/missing.
        Returns number of entries indexed. Non-fatal.
        """
        try:
            if not self._graph_enabled or self._graph is None:
                return 0
            # Reset the graph
            self._graph._nodes.clear()
            self._graph._edges.clear()
            self._graph._loaded = True
            count = 0
            for entry in self.memories:
                n = self._graph.index_entry(entry.hash, entry.content)
                if n > 0:
                    count += 1
            self._graph.save()
            # also rebuild clusters when doing a full graph rebuild
            self.search_engine.rebuild_clusters(self.memories)
            return count
        except Exception:
            return 0

    def rebuild_clusters(self) -> int:
        """Rebuild the cluster index from all loaded memories.

        Call this after bulk ingestion when you want cluster-boosted search
        results immediately, without waiting for the next auto-flush.

        Returns the number of memories indexed into clusters.
        """
        try:
            self.search_engine.rebuild_clusters(self.memories)
            return len(self.memories)
        except Exception:
            return 0