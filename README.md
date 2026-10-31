# Parsica Spirit

## Status, 30 October 2026

During a recent review, flaws in this code were found to have been introduced during the builds, and fixes are being rolled out now. What was found, what is fixed and what remains is in REVIEW-2026-10.md. Both this repository and its sibling will be retired soon. Their successor, Continuum, combines the best of both into a single plugin; when it is released, Spirit and Weave will be retired and archived.

An open-source, open-core port of the Parsica memory engine: persistent, deterministic,
audit-friendly memory for AI agents, with a lightweight Claude Code adapter.

Design principles, non-negotiable: **deterministic, audit-friendly, provenance-first,
every-lever-visible, zero-magic, local-first.** Nothing in this repository makes a
network call, requires an embedding, or touches an external service. The base tier is
markdown and JSONL you can read with your eyes; the engine tier is pure
standard-library Python. One package, zero runtime dependencies.

## What it does

Spirit gives an agent (or any application) a memory it can trust:

- **A scrub-gated write path.** Everything stored passes `scrub -> enrich -> persist`,
  in that order, with no way around it - the same scrub gate fires whether you use the
  `Context` facade or call the `MemorySystem` engine directly. Secret credentials AND the
  four built-in PII patterns (email, phone, US SSN, card) are always redacted, end to end,
  before anything reaches storage or the search index. (The `scrub_pii` parameter on the
  `Context` / `open_markdown` facades is accepted for compatibility but has no effect: the
  storage seam re-scrubs on every write, so PII redaction is not optional.)
- **Deterministic retrieval, scoped honestly.** On the base tier, the same store and the
  same query return the same results, every run - the ranking path is pure token overlap by a
  fixed weight, with no clock. On the engine tier, ranking is reproducible for a fixed store,
  configuration, and reference time, but it deliberately EVOLVES: time decay and access
  reinforcement reshape scores as memories age and get used. `search(..., explain=True)` makes
  the engine ranking explainable - each result carries its matched terms and a score breakdown.
- **An accumulating substrate** (engine tier). Entity graph, co-occurrence statistics,
  windowed and clustered indexes, quality signals. The store gets better to search as
  it grows, and none of it depends on a model, so your memory is never locked to a
  vendor or an embedding version.
- **Workspace-bound identity.** One identity per workspace, scoped once, non-exportable
  by construction. A copied workspace is detected and fails closed loudly instead of
  silently impersonating the original; moving the old identity aside is an explicit follow-up.
- **A deliberate forgetting ladder.** Nothing is destroyed casually: `forget` moves
  records to a recycle bin, `recover` brings them back, and a full reset requires a
  two-step plan-then-execute handshake with an audit trail.

## Features at a glance

Spirit installs as one product. Inside, the engine tier is the same surface as the base
tier with an accumulating substrate underneath; the tier column says which layer provides
each feature. Every row links to details below.

| Feature | Tier | What you get |
| --- | --- | --- |
| [Scrub-gated writes](#store-and-query-base-tier) | base | Secrets AND the four PII patterns (email/phone/US SSN/card) always redacted before disk, end to end - not optional (`scrub_pii` is accepted for compatibility but has no effect). |
| [Store, query, labels](#store-and-query-base-tier) | base | Store and search plain text, filter by labels, fetch by id, dump everything for audits. |
| [Continuity](#continuity-working-set-journal-lessons) | base | A working set, journal, and lessons that survive compaction and restarts. |
| [Workspace identity](#identity-one-per-workspace-non-exportable) | base | One identity per workspace, fail-closed on copies, with an audited recovery path. |
| [Deliberate forgetting](#forgetting-deliberately) | base | Recycle bin, two-step reset, audit trail. Nothing is destroyed casually. |
| [Claude Code import](#connecting-claude-codes-own-memory) | base | Bring your existing `CLAUDE.md` / `MEMORY.md` memory with you. Import-only, idempotent. |
| [Weave promotion and recency](#the-durable-pool-promotion-from-weave) | both | Weave's distilled records land on a session-tagged durable floor, with recency reads and an audited backfill. |
| [Bring-your-own hooks](#hooks-enrichment) | both | Plug in your own enricher. |
| [Search filters and explanations](#search-filters-and-explanations) | engine | Filter by tag, date, category, type, sentiment, confidence. `explain=True` shows why a result ranked. |
| [Workspace, agent, session scoping](#scoping-workspaces-agents-sessions) | engine | One store per project workspace; per-agent isolation with a shared pool; session write-tagging with a cross-session recall dial. |
| [Session isolation](#scoping-workspaces-agents-sessions) | engine | A trigger-based write gate: strict, permissive, or off. Scope a session's writes without giving up shared recall. |
| [Memory types](#memory-types) | engine | Mistakes, facts, preferences, procedures. Each type decays and recalls on its own terms. |
| [Decay and reinforcement](#decay-and-reinforcement) | engine | Unused memories fade on a half-life; what you actually use stays vivid. |
| [Entity graph](#graph-and-associations) | engine | Entities and relationships accumulate from your corpus and boost ranking. No model. |
| [Usage feedback](#usage-feedback) | engine | Mark what helped; retrieval improves where it matters. Explicit and local. |
| [Tiered storage and WAL](#storage-and-durability) | engine | Sharded stores, hot/warm/cold aging in time-window mode, and a crash-safe write-ahead log. |
| [Ingest helpers](#ingesting-content) | engine | Files, directories, bulk batches, `@tags`, auto-categories, noise gates, fact dedup. |
| [Maintenance](#maintenance) | engine | Compact, consolidate, re-enrich, rebuild indexes and clusters. All explicit calls. |
| [Selective forgetting](#selective-forgetting-engine) | engine | Forget by topic, entity, age, or ingest source. Audited, and purged from the log. |
| [Stats, health, migration](#operations-stats-health-migration) | engine | `get_stats()`, `get_health()`, a bootstrap-size guard, versioned schema migration. |
| [Analysis and synthesis](#analysis-and-synthesis) | engine | Themes, consolidated knowledge, research suggestions. Local and deterministic. |
| [Context packets](#context-packets-for-spawned-agents) | engine | Token-budgeted briefing packets for spawned agents, pitfalls included. |
| [Export / import](#portability) | engine | Whole-store JSON round-trip with dedup on re-import. Your memory is a file you can move. |
| [Privacy defaults](#privacy) | both | Local-first, zero network calls, content-free instrumentation, ephemeral query text. |

## What's inside

One package (`parsica_spirit`), three zones, one import direction:

```
facade  ---->  enrich  <----  engine
(the user-facing     (shared contracts        (the accumulating
 surface)             leaf)                    memory engine)
```

- **The facade** (the top-level `parsica_spirit` modules): `Context` (store / query /
  labels / reset ladder / audit), continuity files (working set, journal, lessons),
  identity. Backend-agnostic by dependency inversion - it never imports the engine.
- **The contracts leaf** (`parsica_spirit.enrich`): zero imports from the rest of the
  package. The single source of truth for what an enrichment record is
  (`EnrichmentRecord` / `EnricherPort`), the `StoragePort` / `AdminBackend` protocols
  every backend implements, the shared scrubber both tiers redact through, and the
  conformance suite every backend must pass.
- **The engine** (`parsica_spirit.engine`): the lean engine. Sharded JSON storage with
  hot/cold tiers, 11-layer BM25+ retrieval, the accumulating substrate, decay and
  reinforcement, temporal reasoning, confidence scoring, consolidation, selective
  forgetting. Reaches the facade only through the leaf contracts.
- **scripts/verify_release.py**: the one-command release gate: tests, import
  closure, closure arming (release-side), plain-ASCII docs, clean-room wheel build,
  licensing.

## Dependencies

- Python 3.9+
- **For Claude Code use, Spirit pairs with Parsica Weave** (its own repository,
  linked from the Parsica site). Weave is the capture and continuity skill and works
  standalone; Spirit provides the engine and Python backend. The automatic
  Weave-to-Spirit full-stack bridge is **experimental** in this release: each
  component is complete on its own, and the integration ships as beta.
- Nothing else. One wheel, standard library only: installing Parsica Spirit adds no
  third-party dependency to your environment.

## How to install

From source (PyPI publication is planned; it becomes a plain `pip install parsica-spirit`):

```
git clone <this-repo> parsica-spirit
cd parsica-spirit
pip install .
```

One install, one package - the engine and the contracts leaf are subpackages inside
`parsica_spirit`, not separate distributions, so there is nothing else to choose or add.
The base tier never imports the engine (CI-enforced); escalation is explicit, by injecting
an engine backend (below), so you always know which tier you are running.

Until the pile-one pull requests listed in REVIEW-2026-10.md merge, treat the scrub as best effort and keep sensitive material out.

## How to use it

### Store and query (base tier)

```python
from parsica_spirit import open_markdown

ctx = open_markdown("~/agent-memory/myproject")

r = ctx.store("Decided: retries use exponential backoff, capped at 60s",
              labels={"topic": "architecture"})
print(r.id, r.redactions)        # scrubbed on the way in, always

for hit in ctx.query("backoff policy", limit=5):
    print(hit.id, hit.content)
```

The scrub gate is not optional. Store a string containing a live API key and the
persisted record carries `[REDACTED:...]` in its place; `r.redactions` tells you it
happened.

Labels pass the same scrub gate as content - string keys and values are scrubbed, scalar
non-string values (`int`/`float`/`bool`/`None`) keep their type, and nested containers
(`dict`/`list`/`set`) are rejected because labels are exact-match metadata, not structured
storage. A post-scrub label-key collision with conflicting values raises `ValueError` instead
of silently dropping a key (colliding keys carrying identical values collapse into
one). Query text is ephemeral by contract: it is never persisted, logged, or indexed by
any backend, and the `labels=` filter on `query` is scrubbed symmetrically with
`store`, so a redacted value still matches.

A few more calls on the same surface:

```python
rec = ctx.get(r.id)               # fetch one record by id
rows = ctx.all_records()          # unranked full dump, for audits and iteration
ctx.relabel(r.id, labels={"status": "final"})   # merge labels in place, scrub-gated
```

### Continuity (working set, journal, lessons)

```python
ctx.continuity.save_working_set("## Pinned facts\n- Ship v1 on Friday\n## Open loops\n- fix flaky test")
ctx.continuity.append_lesson("## 2026-07-02: scrub before truncate, always")
ctx.continuity.append_journal("Wired the release gate today.")
print(ctx.continuity.restore())   # everything an agent needs to re-orient, scrubbed
```

`restore()` is what a session-start hook injects: the working set, recent journal, and
lessons survive compaction and restarts.

### Identity (one per workspace, non-exportable)

```python
ident = ctx.scope_identity("ExampleAgent")
print(ctx.identity_banner())      # deterministic banner for session start; '' when unset
print(ctx.byline_directive(session_tag="ab12cd34"))
# -> a directive the host injects so the agent opens every response with "[ExampleAgent · ab12cd34]"
```

A workspace holds exactly one identity; a second `scope_identity` is refused. There is
no export or import path, and a copied workspace fails its fingerprint check and is
flagged loudly rather than silently acting as the original: the identity reads as
absent and the session banner shouts. Identity here is flexible, not cryptographic;
an invalid identity (copied, moved, or corrupt workspace) flags loudly but never
disables the agent, and a workspace that was never scoped is simply unset - the
banner stays empty. An identity file from before workspace binding, one with no
fingerprint, is not usable either: it cannot be told from a copy with that field
blanked, so it does not sign and does not pass the promotion gate until
`rescope_after_move` binds a fresh identity to the workspace (the old file is kept
aside in `.identity_quarantine/`).

The **byline** is the identity's visible face: a one-line signature the agent opens
every response with, carrying the name this workspace gave it (plus the host-supplied
session tag, so an operator running several sessions can tell them apart). The name a
user chose showing up on every reply is what makes choosing it carry weight - for the
user and the agent both. It follows the banner's purity contract (a pure function of
on-disk state plus the supplied tag - byte-identical for the same tag, survives
compaction) and it only speaks
for a usable identity: an unset or unnamed workspace is never nagged, and a copied,
moved, or corrupt workspace gets an empty string - signing is a privilege of an
identity actually bound to this workspace. Prompt-layer by design: the agent signs
because it honors its identity, not because a gate forces it.

The programmatic surface is fail-closed, and quarantine is not a dead end. Moving a
mismatched identity aside is always an explicit step, never automatic:

```python
ctx.identity                      # the Identity when valid and bound; None on a copied,
                                  # moved, or corrupt workspace
ctx.identity_view                 # read-only full classification for the audit path:
                                  # status plus fingerprints, including a quarantined record
ctx.quarantine_identity()         # explicitly move an invalid identity aside (kept, never
                                  # deleted; a no-op returning None while identity is valid)
ctx.rescope_after_move("ExampleAgent")    # the recovery path after a legitimate move, and for
                                  # an identity with no fingerprint: the old identity is
                                  # quarantined aside, a fresh one is minted bound to this
                                  # workspace, and the operation is audited
```

### Forgetting, deliberately

```python
ctx.forget(r.id)                  # moves to the recycle bin, not destruction
print(ctx.recycle_bin())          # list what is sitting in the bin
ctx.recover(r.id)                 # and back

plan = ctx.plan_reset(scope="all", disposition="wipe")   # step 1: a plan with a hash
ctx.execute_reset(plan.plan_hash)                        # step 2: execute that exact plan
print(ctx.audit_log())            # every destructive op, stamped, metadata only
```

No single call destroys data. The two-step handshake and the audit trail make resets
deliberate and reviewable. The engine tier adds targeted forgetting by topic, entity,
age, or source: see [Selective forgetting](#selective-forgetting-engine).

### Escalate to the engine tier

```python
import os
from parsica_spirit import Context
from parsica_spirit.engine.seam import EngineBackend

ctx = Context(EngineBackend(os.path.expanduser("~/agent-memory/myproject")))
```

The facade is identical in both tiers: same `store`, same `query`, same ladder. The
engine adds the accumulating substrate: graph and co-occurrence aware ranking,
windowed and clustered indexes, decay, temporal reasoning, confidence, consolidation.
`ctx.tier` tells you which one you are on.

### Hooks (enrichment)

Both tiers accept a bring-your-own enricher.

```python
ctx = open_markdown("~/agent-memory/myproject", enricher=my_enricher)  # any EnricherPort
r = ctx.store("raw note", enrich=False)                                # skip enrichment per write
```

The enricher always runs on post-scrub clean text and is non-fatal: if it fails, the
write still lands, unenriched. The default is the deterministic doc2query-style
`ReferenceEnricher`, a zero-dependency reference implementation that ships so the
`EnricherPort` contract always has a known-good example and the leaf stays demonstrable with
no model or network. By its own docstring this is NOT the production enricher; for real
enrichment, wire your own `EnricherPort` through the `enricher=` seam shown above. On the
engine, `get_enrichment_count()` tracks how many
enrichment calls the engine's OWN enricher has made (via `ingest` / `re_enrich`);
enrichments injected through the facade are metered by the facade, not this counter.

## The engine in detail (parsica_spirit.engine)

Everything below ships in the engine tier. You normally reach it through the same
`Context` facade; it is also available directly for applications that want the full
surface:

```python
import os
from parsica_spirit.engine import MemorySystem   # the engine memory system

mem = MemorySystem(os.path.expanduser("~/agent-memory/myproject"), agent_name="aria")
```

(Expand `~` yourself: the constructor takes the path as given.)

### Search: filters and explanations

`search()` is ranked, deterministic retrieval with a real filter surface:

```python
hits = mem.search("backoff policy", limit=10,
                  tags=["architecture"], tag_mode="any",
                  date_range=("2026-01-01", "2026-07-01"),
                  category="decisions", memory_type="fact",
                  min_confidence=0.6)
```

Filters apply after scoring, so relevance weights stay correct. The dials:

- `explain=True`: each result carries its matched terms and a score breakdown, so you
  can see exactly why it ranked.
- `confidence_floor=...`: drop low-certainty results instead of returning weak guesses.
- `include_cold=True`: search the cold archive explicitly. By default, colder data
  loads on demand when results run thin.
- `use_decay=True`: apply time decay to ranking at query time.

Recency and time:

```python
mem.recent(limit=20)                    # most recently ACCESSED first, optionally per agent
mem.recall_recent(hours=24)             # recent memories, filterable by session/channel
mem.on_date("2026-07-01")               # what happened that day
mem.between("2026-06-01", "2026-07-01") # a window
mem.narrative("the release")            # a chronological story for a topic
```

Vocabulary bridging is model-free by construction: documents are enriched at write
time (search keywords, summaries, anticipated queries) and those fields are weighted
directly into the index - that alone carries vocabulary bridging, and it keeps short
queries sharp. No embeddings required, nothing learned from your corpus leaves it.

### Scoping: workspaces, agents, sessions

- **Workspaces are the scope.** Memory is rooted in the workspace directory you
  open - one store per project. The same workspace opened from any surface
  (terminal, desktop app, IDE) is the same memory; a different project directory is
  a separate store. Everything below scopes *within* a workspace.
- **Agents.** `MemorySystem(agent_name="aria")` stamps every memory. Agents are
  isolated from each other and share a common pool; omitting `agent_name` warns
  loudly instead of failing silently.
- **Sessions.** Pass `session_id=...` and writes are tagged to that session. Reads
  are a dial: `cross_session_recall="all" | "semantic" | "none"`, and
  `session_id="*"` reads across everything. Writes are always tagged; what a session
  can recall is your choice.
- **Session isolation (the write gate).** `session_awareness="off" | "open" | "closed"`
  adds a trigger-based gate on top of the tagging. `off` (the default) is today's
  behavior. `open` allows everything but logs writes from idle or unregistered sessions.
  `closed` is strict: only a session activated with `begin_turn(session_id)` may
  write, idle and unregistered sessions are blocked, and every session-scoped write
  honors the gate - ingest (including the file, directory, and bulk helpers), the
  typed helpers, forget, synthesize, re-enrichment and re-decomposition.
  Operator-level maintenance calls (`compact`, `delete_source`) take no session and
  are not gated. `end_turn()` returns
  the session to idle; `add_task()` / `complete_task()` keep it active while spawned
  sub-agents finish; `force_idle_session()` is the escape hatch. An invalid mode
  value warns loudly and falls back to `off` - a typo never silently costs you the
  isolation you asked for. Isolation is for writes, not reads: recall stays governed
  by `cross_session_recall`.
- **Channels are a stamp, not a scope.** `source_channel=...` records which surface
  a memory came from, and `search()` / `recall_recent()` can filter by it. Scope
  comes from the workspace; channels only label provenance within it.
- **Namespaces.** Namespace separation is inherited and initialized automatically at
  startup.

### Memory types

```python
mem.ingest_mistake("Assumed the cache was warm; it was not.",
                   "Verify cache state before relying on it.")   # what happened, correction
mem.ingest_fact("The staging DB lives in us-east-2.")
mem.ingest_preference("Operator prefers terse commit messages.")
mem.ingest_procedure("Release: run the gate, then tag, then push.")
```

Each type carries its own decay behavior and recall priority. The built-in configs
are fixed by design; custom types are supported too, via
`ingest(content, memory_type="mytype", type_config={...})` - the sanitized override
(`decay_multiplier`, `importance_boost`, `recall_priority`, `label`) rides the entry
and drives its decay curve and recall ranking. A custom type with no override gets
the episodic defaults.

### Decay and reinforcement

Memories fade on a configurable `half_life`, and search hits reinforce the memories
they return (repeated identical queries served from the read-cache skip
re-reinforcement): what you actually use stays vivid, what you never touch ages
down. Decay influences ranking (`search(use_decay=True)`), not existence: removal only
ever happens through explicit maintenance or the forgetting APIs.

### Graph and associations

With `graph_intelligence=True` (the default), an entity graph and co-occurrence
statistics accumulate from your corpus and boost ranking. The graph is also queryable
on its own:

```python
mem.graph_search("postgres")            # entity-aware search
mem.entity_path("postgres", "backups")  # how two entities connect
mem.get_entity("postgres")              # one entity's record
mem.get_graph_stats()
mem.rebuild_graph()
```

No model and no embeddings: the graph is built from your text, deterministically.

### Usage feedback

A closed retrieval loop, explicit and local:

- `search_with_context(...)` records search context for feedback, and
  `cooccurrence_boost=True` folds the association index into ranking. No model, no
  embeddings.
- `mark_used(...)` and `boost_relevance(...)` tell the store which results actually
  helped.
- `get_hot_entries()` shows what keeps getting used.

### Storage and durability

Three storage modes, selected by constructor flags:

1. **Date + topic shards** (`shards/`, the default): `shard_YYYY-MM_topic.json`
   files, partitioned by month and dominant category. Scales with store size.
2. **Time-window shards** (`use_sharding=False, sharding=True`): weekly
   `memories_YYYY-WXX.jsonl` files.
3. **Legacy flat file**: a single `memory_metadata.json` document, still fully
   supported. (When loading in the time-window configuration, an older flat
   `memories.jsonl` is recognized as a migration source and auto-converts to weekly
   shards; the original file is never modified - it is copied to
   `memories.jsonl.bak` once the conversion is verified.)

Tiering applies in the time-window configuration: the hot tier (~3 days) stays in
memory, warm shards (3 to 14 days) load on demand when a query runs thin, and older
shards are cold - searched only when you pass `include_cold=True`. Tiers classify the
weekly shard files in place; nothing is moved. The default date+topic mode loads the
full store (up to a 75,000-entry safety limit, with a warning when the cap is hit)
and ignores the tiering flags.

Durability: every accepted entry is appended to a write-ahead log before it reaches
permanent shard storage, and the WAL replays on load after a crash. `flush()` and
`close()` are the explicit lifecycle calls.

### Ingesting content

Every free-text ingest path shares the same core gate: JSON-junk rejection, priority
gating (P0 through P3, with P3 dropped), and dedup by hash and normalized content.
(The one exception: `ingest_mistake` stores its structured record with dedup only -
a deliberately recorded mistake is never gated as noise.)
Pipeline and agent-turn sources additionally filter conversational noise - bare
acknowledgments, code lines, table fragments - so automated capture cannot silt up
the store. Dedup drops only exact-hash and normalized-content duplicates; facts that
merely share wording but differ in content are all kept.

```python
mem.ingest_file("notes/decisions.md", category="decisions")
mem.ingest_directory("notes/", category="notes", pattern="*.md")
items = ["note one", "note two"]   # List[str]
mem.ingest_bulk(items)             # one end-of-batch flush; much faster for large batches
```

Tagging: write `@tag` inside content, pass custom `tag_terms=[...]` at construction,
and automatic category tagging fills in the rest.

### Maintenance

Heavy maintenance is explicit - none of the calls below run on their own. (The one
automatic piece: the WAL flushes itself periodically as writes accumulate, and each
flush rebuilds the cluster index.)

```python
mem.compact()                      # dedup + remove decay-dead entries; returns a space report
mem.consolidate()
mem.compress_old(days=7)
mem.re_enrich(batch_size=50)       # idempotent enrichment backfill for older records
mem.re_decompose()                 # extract atomic facts, linked to their parent memory
mem.rebuild_indexes()              # full-text / tag / date indexes (the use_indexing flag)
mem.rebuild_clusters()             # clusters rebuild at flush; call this after bulk ingestion
```

`re_enrich` takes `overwrite=` and a `progress_fn` callback for large stores. The read
cache (`enable_read_cache`, `cache_max_entries`) is an LRU keyed by the full query
signature and invalidated on write, so a cached result can never go stale.

### Selective forgetting (engine)

Distinct from the Spirit recycle-bin ladder: the engine can forget by shape.

```python
mem.forget(topic="old-project")          # or entity=..., or before_date=...
mem.delete_source("https://example.com/doc")   # everything whose ingest was stamped
                                               # with source_url=... (document/web ingests)
```

Both are audited, and the WAL is purged so forgotten content does not survive in the
log. `delete_source` matches the `source_url` stamp from `ingest(..., source_url=...)`;
content brought in via `ingest_file()` is keyed by its source label instead - use the
`forget()` shapes for that.

### Operations: stats, health, migration

```python
mem.get_stats()                    # totals, versions, feature flags, category counts
mem.stats()                        # adds search latency, cache hit rate, hot/warm/cold counts
mem.get_health()                   # readiness check
mem.check_bootstrap_files()        # warns before bootstrap files hit the ~35K char
                                   # injection limit that silently truncates agent context
```

Schema migration runs automatically at startup with a legacy fallback; the manual
surface is `migrate_schema()`, `rollback_migration()`, `validate_data()`, and
`get_migration_history()`.

### Analysis and synthesis

`analyze(query)` summarizes what the store holds on a query: memory count, category
breakdown, average sentiment, date range, and sample excerpts.
`synthesize_knowledge(topic)` returns a
consolidated summary string of what the store knows about a topic (or `None` when
nothing matches); `research_suggestions()` surfaces gaps worth researching next.
`synthesize(research_results=None)` runs a synthesis cycle: with no arguments it
reports knowledge gaps and suggestions, and given a `{source: findings}` dict it
integrates the findings, writing new synthesis-tagged entries into the store. Local and model-free.

### Context packets for spawned agents

`build_context_packet()` and `build_context_packet_multi()` produce token-budgeted,
relevance-filtered briefing packets for a spawned agent, including a Known Pitfalls
block, so a subagent starts with the context that matters instead of the whole store.

### Portability

```python
mem.export("backup.json")
mem.import_from("backup.json", merge=True)
```

A JSON round-trip of the loaded store with hash-based dedup on re-import: your memory
is a file you can move. (Export covers what is loaded: the default mode loads the
full store up to a 75,000-entry safety cap; under time-window tiering, export covers
the actively loaded shards.)

### Privacy

Local-first and network-free is the baseline for the whole repository. On top of it:

- Query text is never persisted - not by `search()`, and not by the instrumented
  `search_with_context()` path. Raw queries stay in memory only.
- Instrumentation is local and content-free: the retrieval/use event log records
  counts, ids, and timestamps in **local-only** files in your workspace - never the
  query text and never memory content.

## Verifying a build

```
python scripts/verify_release.py
```

One command runs the full gate: unit suites, import-closure (the base tier provably
never touches the engine), the closure tests with their closure-armed check (the
exclusion manifest, armed release-side), a plain-ASCII check on the shipped docs, a
clean-room wheel build in a fresh venv, and license checks.

On a public git checkout the closure-armed gate reports red by design: the exclusion
vocabulary is a site-local sidecar, armed release-side and never committed. See
`PROVENANCE.md`.

## Connecting Claude Code's own memory

Parsica Spirit can import Claude Code's native memory, so your existing continuity comes with
you instead of starting over. Claude Code keeps memory as plain markdown: instruction files
(`CLAUDE.md`, `CLAUDE.local.md`, a user-level `~/.claude/CLAUDE.md`) and auto-memory (a
`MEMORY.md` index plus topic files). The importer reads those, splits them by heading into
provenance-stamped records, and stores them through the same scrub + refusal + enrichment path
as everything else:

```python
from parsica_spirit import open_markdown, import_claude_memory
from parsica_spirit.engine.seam import EngineBackend  # or the base tier

ctx = open_markdown("~/agent-memory/myproject")   # or Context(EngineBackend(...))
report = import_claude_memory(ctx, project_root=".", memory_dir="~/.claude/projects/<p>/memory")
print(report.as_dict())   # {added, updated, unchanged, deleted, redactions}
```

It is **import-only and idempotent by construction**: Claude Memory flows into Parsica, never the
reverse (those files are yours), and re-running skips unchanged sections, updates changed ones, and
never duplicates. Every record carries its provenance (`source_kind`, `source_scope`, `heading_path`,
`content_hash`), and the instruction-vs-fact distinction is preserved: a `CLAUDE.md` rule is imported
as an instruction, not promoted to a high-confidence fact unless you promote it. This is the mix that
makes the pairing worth it, and it is offered deliberately.

Imported records form the **native permanence class**. Nothing in the base tier decays them (ranked
retrieval is clock-free by contract), and the ranked query weights them by a fixed constant -
`NATIVE_MEMORY_WEIGHT` in `backends/_common.py`, pinned by test - so an operator-curated note
outranks an equal-overlap session record while a dead-on session hit still wins. Federated reads
surface their origin (`source`, `source_scope`, `heading_path` on every hit), so a native record
always identifies itself at retrieval.

The bridge has two clean directions, and neither knows about any benchmark. A *backend* plugs in
UNDER Spirit through the `StoragePort` / `AdminBackend` contract in `parsica_spirit.enrich` (the base
markdown tier, the engine, or a future store). An external memory *source* feeds IN through a small
reader like `import_claude_memory` above. Sources import; backends inject; the engine stays the
canonical store.

## The durable pool: promotion from Weave

The experimental full-stack bridge has a Spirit side, and this is it. Weave's
`promote` verb calls into `parsica_spirit.promote`: distilled warm memory (journal
entries, lessons, pinned working facts, decision-bearing enriched turns) moves down to
a durable Spirit store shared by every session, one pool per workspace. Promotion is
explicit and idempotent, a planner first and a writer second, never wired to a hook.
Every write crosses the same scrub and refusal path as everything else here, and raw
transcript chatter never promotes: the capture mirror is consulted for two provenance
fields per chunk, never for text.

- **Every record entering the pool carries a session tag.** Promoted records carry
  `source_session` (the eight-character tag of the session that authored the source),
  `created` (store time: when the record entered the pool, UTC), and `event_ts` (when
  the source actually happened, written only when recoverable). Enriched turns join
  their chunk id back to the capture mirror for the real session and timestamp;
  lesson and journal entries carry theirs in the provenance stamp Weave's workspace
  layout writes; a source that cannot testify to its author falls back to the
  promoting session. All three are labels, provenance only: isolation and record
  identity never key on them. On the engine tier, `source_session` also projects onto
  the engine's channel stamp, so engine-level recency filters see it.
- **Two clocks, stated plainly.** Pool recency keys on `created`: promotion time, not
  utterance time. "Just happened" at the durable tier means "recently entered the
  pool", so a memory that sat in a session for hours before promoting reads fresher
  to the pool than it was in conversation. `event_ts` keeps the original time on the
  record, which makes the skew visible instead of hidden.
- **Recency is a retrieval mode, not a tiebreak.** `federated_hits` surfaces
  `session` and `created` on every hit, and
  `federated_recent(spirit_root, scope_id, hours=12, limit=5, exclude_session=None)`
  returns the records whose `created` falls inside the window, newest first. A record
  with no session tag is included, unattributed, never excluded: tags are
  enhancements, never requirements, so records predating session tagging still
  retrieve.
- **Recency slots come out of the retrieval budget, never on top of it.** `limit` is
  the caller's share of its total budget. A caller with fifteen slots that gives
  recency five shows five recent plus ten ranked, not five plus fifteen.
- **The collector knows both Weave layouts.** The legacy layout keeps all four source
  surfaces under one project directory; the workspace layout shares lessons, journal,
  and working sets workspace-wide and keeps enrichment overlays under
  `projects/<key>/`. `collect_candidates` takes an optional `workspace_root` and
  feature-detects the rest, rotation archives included; the default is the legacy
  behavior, so either side of the bridge can upgrade first.

`scripts/backfill_pool.py` brings an existing pool up to this contract: two
operator-invoked subcommands, loud and idempotent, each printing a full manifest, one
line per record. Neither ever unlinks anything. `--spirit-root` is the base directory
holding one store per scope; a scope's store lives at `<base>/<scope-id>`.

```
python scripts/backfill_pool.py backfill --spirit-root <base> --scope <scope-id> --weave-root <weave-store>
python scripts/backfill_pool.py merge-scope --spirit-root <base> --from <old-scope> --into <new-scope>
```

`backfill` gives every record missing `source_session` its best-effort provenance, in
three tiers: enriched turns join back to the capture mirror for the real session and
event timestamp; journal and lesson records recover their event date from their own
record id (the session is unknowable); everything else is labeled `pre-migration`.
Additive only: a label already present is never overwritten, and a second run is a
no-op. `merge-scope` re-homes every record from one scope into another through the
same scrub-gated store path (all labels preserved, plus a `merged_from` label naming
the previous scope), carries the promotion ledger with it (retirements included, so a
retired source never resurrects through a later promote), skips content-identical
duplicates, and finishes by leaving the old directory in place with a marker file:
the files are yours, and so is their deletion.

## What is deliberately not included

The offline layer calibrator, adaptive quality routing, multi-agent team memory,
document-ingest parsers, the MCP server, cloud backends, and cross-format import stay
upstream. The exclusion manifest is a maintainer-local, gitignored file; the closure tests
(`test_closure.py`, armed release-side) enforce it at release time. None of the excluded modules are present in this public tree.

No data files ship at all. The substrate and every expansion index arrive empty
on purpose: they accumulate **your** corpus.

## Provenance

Every layer's lineage (what was ported, what was newly written, and why no data
files ship at all) is recorded in [`PROVENANCE.md`](PROVENANCE.md).

## Licensing

Parsica Spirit (the `parsica-spirit` package) is licensed under **Apache-2.0**,
chosen deliberately for its explicit patent grant.
See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
