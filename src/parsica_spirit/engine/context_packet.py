"""
Context packets - structured memory injection for sub-agent spawning.

Solves the "cold spawn" problem: sub-agents start with zero context,
leading to confident mistakes based on incomplete information.

A ContextPacket bundles relevant memories, environment details, and
task-specific context into a structured block that can be injected
into a sub-agent's prompt at spawn time.

Additional fields
-----------------
* ``pitfalls`` field - list of Known Pitfall strings from mistake memories.
* ``ContextPacketBuilder.build()`` gains ``include_mistakes=True`` parameter.
  When enabled, relevant mistake memories are surfaced in a "Known Pitfalls"
  section with the format:
    Known Pitfall: Last time this was attempted, <what_happened>.
    Correction: <correction>

Usage:
    from parsica_spirit.engine import MemorySystem

    mem = MemorySystem("./workspace")
    mem.load()

    packet = mem.build_context_packet(
        task="Verify package installation in target venv",
        tags=["package", "installation"],
        environment={"venv": ".venv", "python": "3.11"},
        max_memories=10,
        max_tokens=2000,
        include_mistakes=True,   # default True
    )

    prompt = f\""\"
    {packet.render()}

    YOUR TASK: {task_description}
    \""\"

    data = packet.to_dict()
"""

import hashlib
import html
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class ContextPacket:
    """A structured context bundle for sub-agent spawning.

    Attributes:
        task: The task description this packet was built for.
        memories: List of (content, score, source, category) dicts.
        pitfalls: list of 'Known Pitfall: ...' strings.
        environment: Key-value pairs describing the execution environment.
        instructions: Explicit instructions or constraints for the sub-agent.
        metadata: Build metadata (timestamp, query used, memory count).
    """

    task: str
    memories: List[Dict[str, Any]] = field(default_factory=list)
    pitfalls: List[str] = field(default_factory=list)
    environment: Dict[str, str] = field(default_factory=dict)
    instructions: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    source_session: Optional[str] = None  # Session ID that built this packet

    def render(self, fmt: str = "markdown") -> str:
        """Render the packet as injectable text.

        Args:
            fmt: Output format - "markdown" (default) or "xml" or "json".

        Returns:
            Formatted string ready for prompt injection.
        """
        if fmt == "json":
            return json.dumps(self.to_dict(), indent=2)
        elif fmt == "xml":
            return self._render_xml()
        else:
            return self._render_markdown()

    def _render_markdown(self) -> str:
        lines = ["## Context Packet", ""]

        if self.source_session:
            lines.append(f"**Session:** {self.source_session}")
            lines.append("")

        if self.environment:
            lines.append("### Environment")
            for k, v in self.environment.items():
                lines.append(f"- **{k}:** {v}")
            lines.append("")

        if self.instructions:
            lines.append("### Instructions")
            for inst in self.instructions:
                lines.append(f"- {inst}")
            lines.append("")

        # Known Pitfalls section
        if self.pitfalls:
            lines.append("### Known Pitfalls")
            for pitfall in self.pitfalls:
                lines.append(f"- {pitfall}")
            lines.append("")

        if self.memories:
            lines.append("### Relevant Context")
            for i, mem in enumerate(self.memories, 1):
                content = mem["content"]
                source = mem.get("source", "")
                source_tag = f" *(from: {source})*" if source else ""
                lines.append(f"{i}. {content}{source_tag}")
            lines.append("")

        if self.metadata:
            built = self.metadata.get("built_at", "")
            count = self.metadata.get("total_memories_searched", 0)
            lines.append(
                f"*Packet built {built} - searched {count} memories, "
                f"returned {len(self.memories)} relevant.*"
            )

        return "\n".join(lines)

    def _render_xml(self) -> str:
        lines = ["<context_packet>"]

        if self.environment:
            lines.append("  <environment>")
            for k, v in self.environment.items():
                lines.append(
                    f'    <var name="{html.escape(str(k))}">{html.escape(str(v))}</var>'
                )
            lines.append("  </environment>")

        if self.instructions:
            lines.append("  <instructions>")
            for inst in self.instructions:
                lines.append(f"    <instruction>{html.escape(inst)}</instruction>")
            lines.append("  </instructions>")

        if self.pitfalls:
            lines.append("  <known_pitfalls>")
            for p in self.pitfalls:
                lines.append(f"    <pitfall>{html.escape(p)}</pitfall>")
            lines.append("  </known_pitfalls>")

        if self.memories:
            lines.append("  <relevant_context>")
            for mem in self.memories:
                source = html.escape(mem.get("source", ""))
                content = html.escape(mem["content"])
                lines.append(f'    <memory source="{source}">{content}</memory>')
            lines.append("  </relevant_context>")

        lines.append("</context_packet>")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize the packet to a dictionary."""
        d = {
            "task": self.task,
            "memories": self.memories,
            "pitfalls": self.pitfalls,
            "environment": self.environment,
            "instructions": self.instructions,
            "metadata": self.metadata,
        }
        if self.source_session:
            d["source_session"] = self.source_session
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ContextPacket":
        """Deserialize a packet from a dictionary."""
        return cls(
            task=data.get("task", ""),
            memories=data.get("memories", []),
            pitfalls=data.get("pitfalls", []),
            environment=data.get("environment", {}),
            instructions=data.get("instructions", []),
            metadata=data.get("metadata", {}),
            source_session=data.get("source_session"),
        )

    def __len__(self) -> int:
        """Number of memories in the packet."""
        return len(self.memories)

    def __bool__(self) -> bool:
        """True if the packet contains any context."""
        return bool(self.memories or self.pitfalls or self.environment or self.instructions)

    @property
    def estimated_tokens(self) -> int:
        """Rough token estimate for the rendered packet (len/4 heuristic)."""
        return len(self.render()) // 4

    def trim(self, max_tokens: int) -> "ContextPacket":
        """Return a new packet trimmed to fit within a token budget.

        Removes memories from the tail (lowest relevance) first. Pitfalls
        are NEVER trimmed - they represent known failure modes and are the
        highest-priority content in the packet. The budget constraint applies
        only to memories; the returned packet may exceed max_tokens if
        pitfalls alone push it over, but this is intentional: a sub-agent
        must know the known pitfalls even under a tight token budget.
        """
        if self.estimated_tokens <= max_tokens:
            return self

        trimmed = ContextPacket(
            task=self.task,
            memories=list(self.memories),
            pitfalls=list(self.pitfalls),
            environment=dict(self.environment),
            instructions=list(self.instructions),
            metadata=dict(self.metadata),
        )

        # Trim memories only (lowest relevance first - already sorted tail-last)
        # Pitfalls are never trimmed - they are highest priority.
        while trimmed.memories and trimmed.estimated_tokens > max_tokens:
            trimmed.memories.pop()

        trimmed.metadata["trimmed_to"] = max_tokens
        trimmed.metadata["memories_after_trim"] = len(trimmed.memories)
        return trimmed

    def __repr__(self) -> str:
        session_tag = f" session='{self.source_session}'" if self.source_session else ""
        return (
            f"<ContextPacket task='{self.task[:40]}...'"
            f"{session_tag} "
            f"memories={len(self.memories)} pitfalls={len(self.pitfalls)} "
            f"~{self.estimated_tokens} tokens>"
        )


class ContextPacketBuilder:
    """Builds context packets from a MemorySystem.

    Pulls relevant memories via BM25 search, applies filters,
    and packages them with environment and instruction context.

    ``build()`` gains ``include_mistakes=True`` which proactively
    surfaces relevant mistake memories in a "Known Pitfalls" section.

    Args:
        memory_system: A loaded MemorySystem instance.
    """

    def __init__(self, memory_system):
        self.mem = memory_system

    def build(
        self,
        task: str,
        tags: Optional[List[str]] = None,
        category: Optional[str] = None,
        environment: Optional[Dict[str, str]] = None,
        instructions: Optional[List[str]] = None,
        max_memories: int = 15,
        max_tokens: int = 4000,
        min_relevance: float = 0.1,
        include_sources: bool = True,
        include_mistakes: bool = True,
        max_pitfalls: int = 5,
        source_session: Optional[str] = None,
    ) -> ContextPacket:
        """Build a context packet for a sub-agent task.

        Searches the memory store for relevant context and packages it
        into a structured ContextPacket.

        Args:
            task: Description of the sub-agent's task (used as search query).
            tags: Optional tag filters to narrow search.
            category: Optional category filter.
            environment: Key-value pairs for the execution environment.
            instructions: Explicit instructions for the sub-agent.
            max_memories: Maximum memories to include (default 15).
            max_tokens: Token budget for the rendered packet (default 4000).
            min_relevance: Minimum relevance score (0.0-1.0).
            include_sources: Whether to include source attribution (default True).
            include_mistakes: surface relevant mistake memories as
                Known Pitfalls (default True).
            max_pitfalls: cap on pitfall entries (default 5).

        Returns:
            A ContextPacket ready for injection into a sub-agent prompt.
        """
        if min_relevance > 1.0:
            raise ValueError(
                f"min_relevance={min_relevance} looks like a percentage. "
                f"Use 0.0-1.0 scale (e.g. 0.1 = 10%)."
            )

        # -- regular memory search -------------------------------------
        results = self.mem.search(
            query=task,
            limit=max_memories * 3,
            category=category,
            explain=True,
        )

        memories = []
        for r in results:
            if tags and hasattr(r.entry, "tags") and r.entry.tags:
                entry_tags = {et.lower() for et in r.entry.tags}
                if not any(t.lower() in entry_tags for t in tags):
                    continue
            elif tags and (not hasattr(r.entry, "tags") or not r.entry.tags):
                continue
            if r.relevance < min_relevance:
                continue
            if len(memories) >= max_memories:
                break

            mem_dict = {"content": r.content, "score": round(r.score, 3)}
            if include_sources and r.source:
                mem_dict["source"] = r.source
            if hasattr(r.entry, "category") and r.entry.category:
                mem_dict["category"] = r.entry.category
            if hasattr(r.entry, "tags") and r.entry.tags:
                mem_dict["tags"] = r.entry.tags

            memories.append(mem_dict)

        # -- proactive mistake surfacing ---------------------
        pitfalls: List[str] = []
        if include_mistakes:
            pitfalls = self._collect_pitfalls(task, max_pitfalls, min_relevance)

        # -- metadata -------------------------------------------------
        metadata = {
            "built_at": datetime.now().isoformat(),
            "query": task,
            "total_memories_searched": len(self.mem.memories),
            "results_found": len(results),
            "results_included": len(memories),
            "pitfalls_included": len(pitfalls),
        }
        if tags:
            metadata["tag_filter"] = tags
        if category:
            metadata["category_filter"] = category

        packet = ContextPacket(
            task=task,
            memories=memories,
            pitfalls=pitfalls,
            environment=environment or {},
            instructions=instructions or [],
            metadata=metadata,
            source_session=source_session,
        )

        if packet.estimated_tokens > max_tokens:
            packet = packet.trim(max_tokens)

        return packet

    def _collect_pitfalls(
        self,
        task: str,
        max_pitfalls: int,
        min_relevance: float,
    ) -> List[str]:
        """Return a list of pitfall strings from relevant mistake memories.

        Mistakes without structured type_metadata fall back to showing content.
        """
        from .memory_types import format_pitfall_line

        # Get all mistake-type memories
        mistake_entries = [
            m for m in self.mem.memories
            if getattr(m, "memory_type", "episodic") == "mistake"
        ]
        if not mistake_entries:
            return []

        # Score them against the task using BM25
        results = self.mem.search(query=task, limit=max_pitfalls * 3, explain=True)
        mistake_hashes = {m.hash for m in mistake_entries}

        pitfalls = []
        for r in results:
            if r.entry.hash not in mistake_hashes:
                continue
            if r.relevance < min_relevance:
                continue

            type_meta = getattr(r.entry, "type_metadata", {}) or {}
            if type_meta.get("what_happened"):
                pitfalls.append(format_pitfall_line(type_meta))
            else:
                # Fallback: use content verbatim
                pitfalls.append(f"Known Pitfall: {r.entry.content}")

            if len(pitfalls) >= max_pitfalls:
                break

        return pitfalls

    def build_multi(
        self,
        task: str,
        queries: List[str],
        environment: Optional[Dict[str, str]] = None,
        instructions: Optional[List[str]] = None,
        max_memories: int = 15,
        max_tokens: int = 4000,
        min_relevance: float = 0.1,
        include_mistakes: bool = True,
        max_pitfalls: int = 5,
    ) -> ContextPacket:
        """Build a packet from multiple search queries.

        Useful when a task spans multiple topics. Deduplicates results
        by memory hash and merges into a single packet.
        """
        seen_hashes = set()
        all_memories = []
        per_query_limit = max(max_memories // len(queries), 5)

        for query in queries:
            results = self.mem.search(query=query, limit=per_query_limit * 2, explain=True)
            for r in results:
                if r.relevance < min_relevance:
                    continue
                entry_hash = getattr(r.entry, "hash", None)
                if not entry_hash:
                    entry_hash = hashlib.sha1(r.content.encode("utf-8")).hexdigest()
                if entry_hash in seen_hashes:
                    continue
                seen_hashes.add(entry_hash)
                all_memories.append({
                    "content": r.content,
                    "score": round(r.score, 3),
                    "source": r.source if r.source else "",
                    "query": query,
                })

        all_memories.sort(key=lambda m: m["score"], reverse=True)
        all_memories = all_memories[:max_memories]

        # pitfalls across all queries
        pitfalls: List[str] = []
        if include_mistakes:
            seen_pitfalls: set = set()
            for query in queries:
                for p in self._collect_pitfalls(query, max_pitfalls, min_relevance):
                    if p not in seen_pitfalls:
                        seen_pitfalls.add(p)
                        pitfalls.append(p)
            pitfalls = pitfalls[:max_pitfalls]

        metadata = {
            "built_at": datetime.now().isoformat(),
            "queries": queries,
            "total_memories_searched": len(self.mem.memories),
            "results_included": len(all_memories),
            "pitfalls_included": len(pitfalls),
        }

        packet = ContextPacket(
            task=task,
            memories=all_memories,
            pitfalls=pitfalls,
            environment=environment or {},
            instructions=instructions or [],
            metadata=metadata,
        )

        if packet.estimated_tokens > max_tokens:
            packet = packet.trim(max_tokens)

        return packet
