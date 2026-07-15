"""
Nightly Consolidation feature for the Parsica Spirit engine.

Compresses cold-tier memory entries using TF-IDF extractive summarization.
Zero LLM calls, zero new dependencies. Pure stdlib implementation.

Usage::

    from parsica_spirit.engine.consolidation_runner import ConsolidationRunner

    runner = ConsolidationRunner(workspace="/path/to/workspace")
    report = runner.run()  # Full consolidation cycle
    
    # Dry run to see what would be consolidated
    report = runner.run(dry_run=True)
    
    # Agent-specific consolidation
    report = runner.run(agent_id="my-agent")
"""

import json
import math
import os
import shutil
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

from .entry import MemoryEntry
from .shards import ShardManager
from .tiers import TierManager, WARM_DAYS


# Stopwords for TF-IDF processing
STOPWORDS = {
    'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'to', 'of',
    'in', 'for', 'on', 'with', 'at', 'by', 'from', 'and', 'or', 'but', 'it',
    'its', 'this', 'that', 'i', 'we', 'you', 'they', 'he', 'she', 'my',
    'your', 'our', 'their'
}


@dataclass
class ConsolidationReport:
    """Report of consolidation run results."""
    entries_before: int
    entries_after: int
    groups_consolidated: int
    bytes_saved: int
    timestamp: str
    dry_run: bool
    details: List[dict]  # per-group: {"category": ..., "original_count": ..., "summary_preview": ...}
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)


class ConsolidationRunner:
    """Standalone consolidation runner for cold-tier memory entries.
    
    Compresses entries using TF-IDF extractive summarization:
    1. Load cold-tier shards (>14 days old)  
    2. Group entries by category + shared non-stopword token overlap (>= 2 shared tokens)
    3. For each group >3 entries: extract representative summary via TF-IDF
    4. Write consolidated summaries to new shard files
    5. Archive original cold entries (move to .consolidated/)
    6. Write audit log entry
    7. Return ConsolidationReport
    
    Parameters
    ----------
    workspace : str
        Root directory containing memory shards
    dry_run : bool
        If True, analyze what would be consolidated but make no changes
    """
    
    def __init__(self, workspace: str, dry_run: bool = False):
        self.workspace = workspace
        self.dry_run = dry_run
        self.tier_manager = TierManager(workspace)
        self.shard_manager = ShardManager(workspace)
        self.audit_log_path = os.path.join(workspace, ".consolidation_audit.jsonl")
        
    def run(self, agent_id: Optional[str] = None) -> ConsolidationReport:
        """Run full consolidation cycle. Returns report of what changed.
        
        Parameters
        ----------
        agent_id : str, optional
            If provided, only consolidate entries for this specific agent
            
        Returns
        -------
        ConsolidationReport
            Report containing stats and details of consolidation
        """
        try:
            # 1. Load cold-tier shards (>14 days old)
            shard_names = self.shard_manager.list_shards()
            hot, warm, cold = self.tier_manager.classify_shards(shard_names)
            
            cold_entries = []
            for shard_name in cold:
                entries = self.shard_manager._load_shard(shard_name)
                # Filter by agent_id if specified
                if agent_id:
                    entries = [e for e in entries if e.agent_id == agent_id]
                cold_entries.extend(entries)
                
            entries_before = len(cold_entries)
            
            if entries_before == 0:
                # No cold entries to consolidate
                return ConsolidationReport(
                    entries_before=0,
                    entries_after=0,
                    groups_consolidated=0,
                    bytes_saved=0,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    dry_run=self.dry_run,
                    details=[]
                )
            
            # Calculate original size for bytes_saved calculation
            original_size = sum(len(json.dumps(entry.to_dict())) for entry in cold_entries)
            
            # 2. Group entries by category + semantic similarity
            groups = self.group_entries(cold_entries)
            
            # 3. For each group >3 entries: extract representative summary
            consolidated_entries = []
            details = []
            groups_consolidated = 0
            
            for group in groups:
                if len(group) >= 3:
                    # Extract summary for this group
                    summary = self.extract_summary(group)
                    
                    # Create consolidated entry (inherit metadata from first entry)
                    base_entry = group[0]
                    consolidated_entry = MemoryEntry(
                        content=summary,
                        source="consolidation_runner",
                        line=0,
                        category=base_entry.category,
                        created=datetime.now(timezone.utc).isoformat(),
                        memory_type="consolidated",
                        session_id=base_entry.session_id,
                        agent_id=base_entry.agent_id
                    )
                    consolidated_entry.importance = max(e.importance for e in group)
                    consolidated_entry.confidence = sum(e.confidence for e in group) / len(group)
                    consolidated_entry.type_metadata = {
                        "consolidated_from": len(group),
                        "original_hashes": [e.hash for e in group],
                        "consolidation_timestamp": consolidated_entry.created
                    }
                    
                    consolidated_entries.append(consolidated_entry)
                    groups_consolidated += 1
                    
                    details.append({
                        "category": base_entry.category,
                        "original_count": len(group),
                        "summary_preview": summary[:100] + "..." if len(summary) > 100 else summary
                    })
                else:
                    # Keep ungrouped entries as-is
                    consolidated_entries.extend(group)
            
            entries_after = len(consolidated_entries)
            new_size = sum(len(json.dumps(entry.to_dict())) for entry in consolidated_entries)
            bytes_saved = original_size - new_size
            
            if not self.dry_run:
                # 4. Write consolidated summaries to new shard files
                self._write_consolidated_entries(consolidated_entries, cold)
                
                # 5. Archive original cold entries
                self._archive_cold_shards(cold, agent_id)
            
            # 6. Create and log report
            report = ConsolidationReport(
                entries_before=entries_before,
                entries_after=entries_after,
                groups_consolidated=groups_consolidated,
                bytes_saved=bytes_saved,
                timestamp=datetime.now(timezone.utc).isoformat(),
                dry_run=self.dry_run,
                details=details
            )
            
            # 7. Write audit log entry
            self._write_audit_log(report)
            
            return report
            
        except Exception as e:
            # Non-fatal everywhere - return error report
            return ConsolidationReport(
                entries_before=0,
                entries_after=0,
                groups_consolidated=0,
                bytes_saved=0,
                timestamp=datetime.now(timezone.utc).isoformat(),
                dry_run=self.dry_run,
                details=[{"error": str(e)}]
            )
    
    def extract_summary(self, entries: List[MemoryEntry], max_sentences: int = 3) -> str:
        """TF-IDF extractive summarization. Zero external deps.
        Score each sentence by sum of TF-IDF weights, return top N.
        
        Parameters
        ----------
        entries : List[MemoryEntry]
            Entries to summarize
        max_sentences : int
            Maximum number of sentences to return in summary
            
        Returns
        -------
        str
            Extractive summary of the entries
        """
        if not entries:
            return ""
            
        if len(entries) == 1:
            return entries[0].content
        
        # Extract all sentences from all entries
        all_sentences = []
        for entry in entries:
            # Simple sentence splitting on periods, exclamations, questions
            sentences = [s.strip() for s in entry.content.replace('!', '.').replace('?', '.').split('.') if s.strip()]
            all_sentences.extend(sentences)
        
        if not all_sentences:
            return ""
            
        if len(all_sentences) <= max_sentences:
            return '. '.join(all_sentences)
        
        # Tokenize sentences and build term frequency
        sentence_tokens = []
        all_tokens = []
        
        for sentence in all_sentences:
            tokens = [word.lower() for word in sentence.split() if word.lower() not in STOPWORDS]
            sentence_tokens.append(tokens)
            all_tokens.extend(tokens)
        
        if not all_tokens:
            return '. '.join(all_sentences[:max_sentences])
        
        # Calculate TF-IDF scores
        total_docs = len(sentence_tokens)
        doc_freq = Counter()
        
        # Count document frequency for each term
        for tokens in sentence_tokens:
            unique_tokens = set(tokens)
            for token in unique_tokens:
                doc_freq[token] += 1
        
        # Score each sentence
        sentence_scores = []
        for i, tokens in enumerate(sentence_tokens):
            if not tokens:
                sentence_scores.append((0, i))
                continue
                
            tf = Counter(tokens)
            max_tf = max(tf.values())
            
            score = 0
            for token in tokens:
                # Normalized term frequency
                tf_norm = tf[token] / max_tf
                # Inverse document frequency
                idf = math.log(total_docs / max(1, doc_freq[token]))
                score += tf_norm * idf
            
            sentence_scores.append((score, i))
        
        # Sort by score and take top max_sentences
        sentence_scores.sort(reverse=True)
        top_indices = sorted([idx for _, idx in sentence_scores[:max_sentences]])
        
        return '. '.join(all_sentences[i] for i in top_indices)
    
    def group_entries(self, entries: List[MemoryEntry]) -> List[List[MemoryEntry]]:
        """Group entries by category first, then by shared non-stopword token overlap (>= 2 shared tokens).
        Minimum group size: 3 entries to consolidate.
        
        Parameters
        ----------
        entries : List[MemoryEntry]
            Entries to group
            
        Returns
        -------
        List[List[MemoryEntry]]
            Groups of similar entries
        """
        if not entries:
            return []
        
        # First group by category
        category_groups = defaultdict(list)
        for entry in entries:
            category_groups[entry.category].append(entry)
        
        # Within each category, group by similarity
        final_groups = []
        
        for category, cat_entries in category_groups.items():
            if len(cat_entries) < 3:
                # Not enough entries to consolidate, keep as individual groups
                for entry in cat_entries:
                    final_groups.append([entry])
                continue
            
            # Group by similarity within category
            similarity_groups = self._group_by_similarity(cat_entries)
            final_groups.extend(similarity_groups)
        
        return final_groups
    
    def _group_by_similarity(self, entries: List[MemoryEntry]) -> List[List[MemoryEntry]]:
        """Group entries by shared non-stopword token overlap.
        Two entries are "similar" if they share >= 2 non-stopword tokens.
        """
        if not entries:
            return []
        
        groups = []
        used = set()
        
        for i, entry1 in enumerate(entries):
            if i in used:
                continue
                
            group = [entry1]
            used.add(i)
            
            # Find similar entries
            tokens1 = set(word.lower() for word in entry1.content.split() 
                         if word.lower() not in STOPWORDS and len(word) > 1)
            
            for j, entry2 in enumerate(entries[i+1:], i+1):
                if j in used:
                    continue
                    
                tokens2 = set(word.lower() for word in entry2.content.split()
                             if word.lower() not in STOPWORDS and len(word) > 1)
                
                # Check similarity: >= 2 shared non-stopword tokens
                shared = tokens1 & tokens2
                if len(shared) >= 2:
                    group.append(entry2)
                    used.add(j)
            
            groups.append(group)
        
        return groups
    
    def _write_consolidated_entries(self, entries: List[MemoryEntry], original_cold_shards: List[str]):
        """Write consolidated entries to new shard files."""
        try:
            for entry in entries:
                self.shard_manager.write_entry(entry)
        except Exception:
            # Non-fatal
            pass
    
    def _archive_cold_shards(self, cold_shard_names: List[str], agent_id: Optional[str] = None):
        """Archive original cold entries to .consolidated/ directory."""
        try:
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")
            archive_dir = os.path.join(self.workspace, ".consolidated", timestamp)
            os.makedirs(archive_dir, exist_ok=True)
            
            for shard_name in cold_shard_names:
                src_path = os.path.join(self.workspace, shard_name)
                if os.path.exists(src_path):
                    if agent_id:
                        # If agent scoping, only archive entries for this agent
                        # Load, filter, and write filtered version
                        entries = self.shard_manager._load_shard(shard_name)
                        agent_entries = [e for e in entries if e.agent_id == agent_id]
                        other_entries = [e for e in entries if e.agent_id != agent_id]
                        
                        # Archive agent entries
                        if agent_entries:
                            archive_path = os.path.join(archive_dir, shard_name)
                            with open(archive_path, 'w', encoding='utf-8') as f:
                                for entry in agent_entries:
                                    f.write(json.dumps(entry.to_dict()) + '\n')
                        
                        # Rewrite original with non-agent entries
                        if other_entries:
                            with open(src_path, 'w', encoding='utf-8') as f:
                                for entry in other_entries:
                                    f.write(json.dumps(entry.to_dict()) + '\n')
                        else:
                            # No other entries, remove the shard
                            os.remove(src_path)
                    else:
                        # Archive entire shard
                        shutil.copy2(src_path, archive_dir)
                        os.remove(src_path)
        except Exception:
            # Non-fatal
            pass
    
    def _write_audit_log(self, report: ConsolidationReport):
        """Write audit log entry (append-only)."""
        try:
            log_entry = {
                "ts": report.timestamp,
                "report": report.to_dict(),
                "dry_run": report.dry_run
            }
            with open(self.audit_log_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(log_entry) + '\n')
        except Exception:
            # Non-fatal
            pass
    
    def get_audit_log(self) -> List[dict]:
        """Return full audit log."""
        try:
            if not os.path.exists(self.audit_log_path):
                return []
            
            log_entries = []
            with open(self.audit_log_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            log_entries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
            return log_entries
        except Exception:
            return []
    
    def undo_last(self) -> bool:
        """Restore entries from last .consolidated/ archive.
        
        Returns
        -------
        bool
            True if successful, False otherwise
        """
        try:
            consolidated_dir = os.path.join(self.workspace, ".consolidated")
            if not os.path.exists(consolidated_dir):
                return False
            
            # Find most recent archive
            archives = [d for d in os.listdir(consolidated_dir) 
                       if os.path.isdir(os.path.join(consolidated_dir, d))]
            if not archives:
                return False
            
            latest_archive = max(archives)
            archive_path = os.path.join(consolidated_dir, latest_archive)
            
            # Restore all files from archive
            for filename in os.listdir(archive_path):
                src = os.path.join(archive_path, filename)
                dst = os.path.join(self.workspace, filename)
                if os.path.isfile(src):
                    shutil.copy2(src, dst)
            
            # Remove the archive directory
            shutil.rmtree(archive_path)
            return True
            
        except Exception:
            return False