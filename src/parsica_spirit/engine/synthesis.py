"""
Knowledge Synthesis - Autonomous research and knowledge integration.

During quiet periods, this engine:
1. Analyzes existing memories to find knowledge gaps
2. Suggests research topics based on incomplete information
3. Integrates new research findings with existing knowledge
4. Creates compound knowledge entries from cross-referenced information
"""

import re
from collections import defaultdict
from datetime import datetime
from typing import Dict, List

from .entry import MemoryEntry


class KnowledgeSynthesizer:
    """Autonomous knowledge synthesis and gap analysis engine."""

    def __init__(self):
        # Question patterns that indicate knowledge gaps
        self._question_patterns = [
            r'what.*(?:is|are|does|would|should|could)',
            r'how.*(?:do|does|can|could|should|would)',
            r'why.*(?:is|are|does|do|would|should)',
            r'when.*(?:will|would|should|does|do)',
            r'where.*(?:is|are|does|would|should)',
            r'which.*(?:is|are|would|should|could)',
        ]
        
        # TODO/incomplete indicators
        self._todo_patterns = [
            r'todo|to\s*do|fixme|hack|temporary',
            r'need\s+to|should|must|require[ds]?',
            r'incomplete|unfinished|pending|placeholder',
            r'tbd|tbh|not\s+sure|unknown|unclear',
        ]
        
        # Reference patterns (mentions without explanation)
        self._reference_patterns = [
            r'(?:using|with|via|through)\s+(\w+(?:\s+\w+){0,2})',
            r'(?:see|check|refer\s+to|mentioned)\s+(\w+(?:\s+\w+){0,2})',
            r'(?:as\s+(?:per|in)|according\s+to)\s+(\w+(?:\s+\w+){0,2})',
        ]

    def identify_gaps(self, memories: List[MemoryEntry]) -> List[str]:
        """Analyze memories to find topics mentioned but not well-covered.
        
        Args:
            memories: List of memory entries to analyze
            
        Returns:
            List of gap descriptions/topics that need more information
        """
        gaps = []
        
        # Track mentioned terms and their coverage
        term_mentions = defaultdict(int)
        term_explanations = defaultdict(int)
        questions = []
        todos = []
        references = set()
        
        for memory in memories:
            content = memory.content.lower()
            
            # Find explicit questions
            for pattern in self._question_patterns:
                matches = re.findall(pattern, content, re.IGNORECASE)
                if matches:
                    questions.append(memory.content.strip())
            
            # Find TODOs and incomplete items
            for pattern in self._todo_patterns:
                if re.search(pattern, content, re.IGNORECASE):
                    todos.append(memory.content.strip())
            
            # Find references to external concepts
            for pattern in self._reference_patterns:
                matches = re.findall(pattern, content, re.IGNORECASE)
                references.update(matches)
            
            # Track technical terms and concepts
            technical_terms = re.findall(r'\b(?:[A-Z]{2,}|[A-Z][a-z]+(?:[A-Z][a-z]*)*)\b', memory.content)
            for term in technical_terms:
                if len(term) > 2:
                    term_mentions[term.lower()] += 1
                    # Check if this memory explains the term
                    if any(word in content for word in ['is', 'means', 'refers', 'defined']):
                        term_explanations[term.lower()] += 1
        
        # Identify terms mentioned but not explained
        unexplained = []
        for term, mentions in term_mentions.items():
            explanations = term_explanations.get(term, 0)
            if mentions >= 2 and explanations == 0 and len(term) > 3:
                unexplained.append(f"What is {term}? (mentioned {mentions} times)")
        
        # Compile gap report
        if questions:
            gaps.append(f"Unanswered questions found: {len(questions)} questions need research")
        
        if todos:
            gaps.append(f"Incomplete items: {len(todos)} TODO/pending items identified")
        
        if unexplained:
            gaps.extend(unexplained[:5])  # Top 5 unexplained terms
        
        if references:
            ref_list = list(references)[:3]  # Top 3 references
            gaps.append(f"External references need context: {', '.join(ref_list)}")
        
        return gaps[:10]  # Return top 10 gaps

    def synthesize(self, memories: List[MemoryEntry], new_info: str, source: str) -> List[MemoryEntry]:
        """Integrate new research findings with existing memories.
        
        Args:
            memories: Existing memory entries
            new_info: New research/information to integrate
            source: Source of the new information
            
        Returns:
            List of new synthesized MemoryEntry objects
        """
        synthesized = []
        
        # Split new info into chunks
        info_chunks = [chunk.strip() for chunk in new_info.split('\n') if len(chunk.strip()) > 20]
        
        for i, chunk in enumerate(info_chunks):
            # Find related memories
            related_memories = self._find_related(chunk, memories)
            
            if related_memories:
                # Create synthesis entry
                related_content = '; '.join([m.content[:100] for m in related_memories[:3]])
                synthesized_content = f"SYNTHESIS: {chunk} [Related: {related_content}]"
                
                entry = MemoryEntry(
                    content=synthesized_content,
                    source=f"synthesis:{source}",
                    line=i,
                    category="tactical",
                    created=datetime.now().isoformat()
                )
                entry.importance = 1.2  # Slightly higher importance for synthesized knowledge
                entry.confidence = 0.7
                entry.tags = ["synthesis", "research"]
                entry.related = [m.hash for m in related_memories]
                
                synthesized.append(entry)
            else:
                # Create standalone entry for new information
                entry = MemoryEntry(
                    content=f"RESEARCH: {chunk}",
                    source=f"research:{source}",
                    line=i,
                    category="tactical",
                    created=datetime.now().isoformat()
                )
                entry.importance = 1.0
                entry.confidence = 0.6
                entry.tags = ["research", "new"]
                
                synthesized.append(entry)
        
        return synthesized

    def suggest_research_topics(self, memories: List[MemoryEntry], limit: int = 5) -> List[Dict]:
        """Suggest research topics based on memory analysis.
        
        Args:
            memories: Memory entries to analyze
            limit: Maximum number of suggestions to return
            
        Returns:
            List of research suggestions with topic, reason, and priority
        """
        suggestions = []
        gaps = self.identify_gaps(memories)
        
        # Convert gaps to research topics
        for gap in gaps:
            if "unanswered questions" in gap.lower():
                suggestions.append({
                    "topic": "Answer outstanding questions",
                    "reason": gap,
                    "priority": "P1"
                })
            elif "incomplete items" in gap.lower():
                suggestions.append({
                    "topic": "Complete pending tasks",
                    "reason": gap,
                    "priority": "P1"
                })
            elif "what is" in gap.lower():
                term = gap.split("What is ")[1].split("?")[0]
                suggestions.append({
                    "topic": f"Research definition of {term}",
                    "reason": gap,
                    "priority": "P2"
                })
            elif "external references" in gap.lower():
                suggestions.append({
                    "topic": "Research external references",
                    "reason": gap,
                    "priority": "P2"
                })
        
        # Add topic-based suggestions
        topic_clusters = self._cluster_topics(memories)
        for topic, count in topic_clusters.items():
            if count >= 3:  # Topics with multiple mentions
                suggestions.append({
                    "topic": f"Deep dive into {topic}",
                    "reason": f"Topic mentioned {count} times - may benefit from comprehensive research",
                    "priority": "P2"
                })
        
        return suggestions[:limit]

    def run_cycle(self, memories: List[MemoryEntry], research_results: Dict = None) -> Dict:
        """Run complete synthesis cycle.
        
        Args:
            memories: Current memory entries
            research_results: Optional dict with research findings to integrate
            
        Returns:
            Report dict with gaps, suggestions, and synthesis results
        """
        report = {
            "timestamp": datetime.now().isoformat(),
            "gaps_identified": [],
            "research_suggestions": [],
            "synthesized_entries": 0,
            "integration_report": None
        }
        
        # Identify knowledge gaps
        gaps = self.identify_gaps(memories)
        report["gaps_identified"] = gaps
        
        # Generate research suggestions
        suggestions = self.suggest_research_topics(memories)
        report["research_suggestions"] = suggestions
        
        # Process research results if provided
        if research_results:
            new_entries = []
            for source, info in research_results.items():
                synthesized = self.synthesize(memories, info, source)
                new_entries.extend(synthesized)
            
            report["synthesized_entries"] = len(new_entries)
            report["integration_report"] = f"Integrated {len(new_entries)} new knowledge entries from {len(research_results)} sources"
            report["new_entries"] = [entry.to_dict() for entry in new_entries]
        
        return report

    def _find_related(self, text: str, memories: List[MemoryEntry]) -> List[MemoryEntry]:
        """Find memories related to the given text."""
        text_lower = text.lower()
        text_words = set(re.findall(r'\w{4,}', text_lower))  # Words 4+ chars
        
        related = []
        for memory in memories:
            memory_words = set(re.findall(r'\w{4,}', memory.content.lower()))
            overlap = len(text_words & memory_words)
            if overlap >= 2:  # At least 2 shared significant words
                related.append((memory, overlap))
        
        # Sort by overlap and return top matches
        related.sort(key=lambda x: x[1], reverse=True)
        return [mem for mem, _ in related[:5]]

    def _cluster_topics(self, memories: List[MemoryEntry]) -> Dict[str, int]:
        """Identify topic clusters in memories."""
        topic_counts = defaultdict(int)
        
        for memory in memories:
            # Extract potential topics (capitalized terms, technical terms)
            topics = re.findall(r'\b[A-Z][a-z]+(?:[A-Z][a-z]*)*\b', memory.content)
            topics.extend(memory.tags)
            
            for topic in topics:
                if len(topic) > 3:
                    topic_counts[topic.lower()] += 1
        
        # Filter to meaningful clusters (3+ mentions)
        return {topic: count for topic, count in topic_counts.items() if count >= 3}