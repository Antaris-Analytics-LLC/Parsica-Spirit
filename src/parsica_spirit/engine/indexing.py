"""
File-based Indexing System - Fast search without loading all memories.

Creates lightweight indexes for:
- Full-text search (word-based inverted index)
- Tag-based filtering
- Date range queries
- Category filtering

All indexes are stored as JSON files for transparency and debuggability.
"""

import json
import os
import re
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

from .entry import MemoryEntry


class SearchIndex:
    """Full-text search index using inverted indexing."""
    
    def __init__(self, workspace: str):
        self.workspace = workspace
        self.index_path = os.path.join(workspace, "search_index.json")
        
        # word -> {memory_hash -> relevance_score}
        self.word_index: Dict[str, Dict[str, float]] = {}
        
        # memory_hash -> {word -> term_frequency}
        self.term_frequencies: Dict[str, Dict[str, int]] = {}
        
        # memory_hash -> basic metadata for search results
        self.memory_metadata: Dict[str, Dict] = {}
        
        self.load_index()
    
    def _extract_words(self, text: str) -> List[str]:
        """Extract searchable words from text."""
        # Convert to lowercase and extract words (3+ chars)
        words = re.findall(r'\b[a-z]{3,}\b', text.lower())
        
        # Filter common stop words
        stop_words = {
            'the', 'and', 'for', 'are', 'but', 'not', 'you', 'all', 'can', 'had', 
            'was', 'one', 'our', 'out', 'day', 'get', 'has', 'her', 'his', 'how', 
            'its', 'may', 'new', 'now', 'old', 'see', 'two', 'way', 'who', 'boy',
            'did', 'doe', 'end', 'few', 'got', 'let', 'man', 'men', 'put', 'say',
            'she', 'too', 'use'
        }
        
        return [w for w in words if w not in stop_words]
    
    def add_memory(self, memory: MemoryEntry):
        """Add a memory to the search index."""
        memory_hash = memory.hash
        
        # Extract words from content
        content_words = self._extract_words(memory.content)
        
        # Extract words from tags and category
        tag_words = []
        for tag in memory.tags:
            tag_words.extend(self._extract_words(tag))
        
        if memory.category:
            tag_words.extend(self._extract_words(memory.category))
        
        # Combine all words with different weights
        all_words = content_words + tag_words * 2  # Tags get double weight
        
        # Calculate term frequencies
        word_counts = defaultdict(int)
        for word in all_words:
            word_counts[word] += 1
        
        self.term_frequencies[memory_hash] = dict(word_counts)
        
        # Add to inverted index with TF scores
        total_words = len(all_words)
        for word, count in word_counts.items():
            if word not in self.word_index:
                self.word_index[word] = {}
            
            # Simple TF score (could be enhanced with TF-IDF)
            tf_score = count / total_words if total_words > 0 else 0
            self.word_index[word][memory_hash] = tf_score
        
        # Store metadata for search results
        self.memory_metadata[memory_hash] = {
            "content_preview": memory.content[:200],
            "category": memory.category,
            "tags": memory.tags,
            "created_at": memory.created,
            "confidence": memory.confidence,
            "source": memory.source
        }
    
    def remove_memory(self, memory_hash: str):
        """Remove a memory from the search index."""
        if memory_hash not in self.term_frequencies:
            return
        
        # Remove from word index
        for word in self.term_frequencies[memory_hash]:
            if word in self.word_index and memory_hash in self.word_index[word]:
                del self.word_index[word][memory_hash]
                
                # Clean up empty entries
                if not self.word_index[word]:
                    del self.word_index[word]
        
        # Remove metadata
        if memory_hash in self.term_frequencies:
            del self.term_frequencies[memory_hash]
        if memory_hash in self.memory_metadata:
            del self.memory_metadata[memory_hash]
    
    def search(self, query: str, limit: int = 50) -> List[Tuple[str, float]]:
        """Search for memories matching the query.
        
        Returns list of (memory_hash, relevance_score) tuples.
        """
        query_words = self._extract_words(query)
        if not query_words:
            return []
        
        # Score memories based on query word matches
        memory_scores = defaultdict(float)
        
        for word in query_words:
            if word in self.word_index:
                for memory_hash, tf_score in self.word_index[word].items():
                    memory_scores[memory_hash] += tf_score
        
        # Sort by relevance score
        results = sorted(memory_scores.items(), key=lambda x: x[1], reverse=True)
        return results[:limit]
    
    def get_memory_metadata(self, memory_hash: str) -> Optional[Dict]:
        """Get metadata for a memory by its hash."""
        return self.memory_metadata.get(memory_hash)
    
    def save_index(self):
        """Save the search index to disk."""
        data = {
            "version": "0.4.0",
            "updated_at": datetime.now().isoformat(),
            "total_memories": len(self.memory_metadata),
            "total_words": len(self.word_index),
            "word_index": self.word_index,
            "term_frequencies": self.term_frequencies,
            "memory_metadata": self.memory_metadata
        }
        
        from .utils import atomic_write_json
        atomic_write_json(self.index_path, data)
    
    def load_index(self):
        """Load the search index from disk."""
        if not os.path.exists(self.index_path):
            return
        
        try:
            with open(self.index_path) as f:
                data = json.load(f)
            
            self.word_index = data.get("word_index", {})
            self.term_frequencies = data.get("term_frequencies", {})
            self.memory_metadata = data.get("memory_metadata", {})
            
        except (json.JSONDecodeError, IOError):
            # Reset to empty state if index is corrupted
            self.word_index = {}
            self.term_frequencies = {}
            self.memory_metadata = {}
    
    def get_stats(self) -> Dict:
        """Get search index statistics."""
        word_counts = [len(memories) for memories in self.word_index.values()]
        
        return {
            "total_memories": len(self.memory_metadata),
            "total_words": len(self.word_index),
            "avg_memories_per_word": sum(word_counts) / len(word_counts) if word_counts else 0,
            "max_memories_per_word": max(word_counts) if word_counts else 0,
            "index_size_bytes": os.path.getsize(self.index_path) if os.path.exists(self.index_path) else 0
        }


class TagIndex:
    """Index for fast tag-based filtering."""
    
    def __init__(self, workspace: str):
        self.workspace = workspace
        self.index_path = os.path.join(workspace, "tag_index.json")
        
        # tag -> [memory_hash, ...]
        self.tag_to_memories: Dict[str, List[str]] = defaultdict(list)
        
        # memory_hash -> [tag, ...]
        self.memory_to_tags: Dict[str, List[str]] = {}
        
        self.load_index()
    
    def add_memory(self, memory: MemoryEntry):
        """Add a memory to the tag index."""
        memory_hash = memory.hash
        
        # Remove old tags for this memory
        if memory_hash in self.memory_to_tags:
            for old_tag in self.memory_to_tags[memory_hash]:
                if old_tag in self.tag_to_memories:
                    self.tag_to_memories[old_tag] = [
                        h for h in self.tag_to_memories[old_tag] if h != memory_hash
                    ]
        
        # Add new tags
        all_tags = memory.tags.copy()
        if memory.category:
            all_tags.append(f"category:{memory.category}")
        
        self.memory_to_tags[memory_hash] = all_tags
        
        for tag in all_tags:
            self.tag_to_memories[tag].append(memory_hash)
    
    def remove_memory(self, memory_hash: str):
        """Remove a memory from the tag index."""
        if memory_hash not in self.memory_to_tags:
            return
        
        # Remove from all tag lists
        for tag in self.memory_to_tags[memory_hash]:
            if tag in self.tag_to_memories:
                self.tag_to_memories[tag] = [
                    h for h in self.tag_to_memories[tag] if h != memory_hash
                ]
                
                # Clean up empty tags
                if not self.tag_to_memories[tag]:
                    del self.tag_to_memories[tag]
        
        del self.memory_to_tags[memory_hash]
    
    def get_memories_by_tag(self, tag: str) -> List[str]:
        """Get all memory hashes that have a specific tag."""
        return self.tag_to_memories.get(tag, [])
    
    def get_memories_by_tags(self, tags: List[str], mode: str = "any") -> List[str]:
        """Get memories that match tags.
        
        Args:
            tags: List of tags to match
            mode: "any" (OR) or "all" (AND)
        """
        if not tags:
            return []
        
        if mode == "any":
            # Union of all tag results
            result = set()
            for tag in tags:
                result.update(self.tag_to_memories.get(tag, []))
            return list(result)
        
        elif mode == "all":
            # Intersection of all tag results
            if not tags:
                return []
            
            result = set(self.tag_to_memories.get(tags[0], []))
            for tag in tags[1:]:
                result &= set(self.tag_to_memories.get(tag, []))
            return list(result)
        
        else:
            raise ValueError(f"Invalid mode: {mode}. Use 'any' or 'all'")
    
    def get_all_tags(self) -> List[Tuple[str, int]]:
        """Get all tags with their memory counts."""
        return [(tag, len(memories)) for tag, memories in self.tag_to_memories.items()]
    
    def save_index(self):
        """Save the tag index to disk."""
        data = {
            "version": "0.4.0",
            "updated_at": datetime.now().isoformat(),
            "total_tags": len(self.tag_to_memories),
            "total_memories": len(self.memory_to_tags),
            "tag_to_memories": dict(self.tag_to_memories),
            "memory_to_tags": self.memory_to_tags
        }
        
        from .utils import atomic_write_json
        atomic_write_json(self.index_path, data)
    
    def load_index(self):
        """Load the tag index from disk."""
        if not os.path.exists(self.index_path):
            return
        
        try:
            with open(self.index_path) as f:
                data = json.load(f)
            
            self.tag_to_memories = defaultdict(list, data.get("tag_to_memories", {}))
            self.memory_to_tags = data.get("memory_to_tags", {})
            
        except (json.JSONDecodeError, IOError):
            # Reset to empty state if index is corrupted
            self.tag_to_memories = defaultdict(list)
            self.memory_to_tags = {}


class DateIndex:
    """Index for fast date-range queries."""
    
    def __init__(self, workspace: str):
        self.workspace = workspace
        self.index_path = os.path.join(workspace, "date_index.json")
        
        # date (YYYY-MM-DD) -> [memory_hash, ...]
        self.date_to_memories: Dict[str, List[str]] = defaultdict(list)
        
        # memory_hash -> date
        self.memory_to_date: Dict[str, str] = {}
        
        self.load_index()
    
    def add_memory(self, memory: MemoryEntry):
        """Add a memory to the date index."""
        memory_hash = memory.hash
        date = memory.created[:10]  # Extract YYYY-MM-DD
        
        # Remove from old date if it existed
        if memory_hash in self.memory_to_date:
            old_date = self.memory_to_date[memory_hash]
            if old_date in self.date_to_memories:
                self.date_to_memories[old_date] = [
                    h for h in self.date_to_memories[old_date] if h != memory_hash
                ]
        
        # Add to new date
        self.memory_to_date[memory_hash] = date
        self.date_to_memories[date].append(memory_hash)
    
    def remove_memory(self, memory_hash: str):
        """Remove a memory from the date index."""
        if memory_hash not in self.memory_to_date:
            return
        
        date = self.memory_to_date[memory_hash]
        if date in self.date_to_memories:
            self.date_to_memories[date] = [
                h for h in self.date_to_memories[date] if h != memory_hash
            ]
            
            # Clean up empty dates
            if not self.date_to_memories[date]:
                del self.date_to_memories[date]
        
        del self.memory_to_date[memory_hash]
    
    def get_memories_in_range(self, start_date: str, end_date: str) -> List[str]:
        """Get all memory hashes in a date range (inclusive)."""
        result = []
        
        for date, memories in self.date_to_memories.items():
            if start_date <= date <= end_date:
                result.extend(memories)
        
        return result
    
    def get_memories_by_date(self, date: str) -> List[str]:
        """Get all memory hashes for a specific date."""
        return self.date_to_memories.get(date, [])
    
    def get_date_distribution(self) -> Dict[str, int]:
        """Get count of memories per date."""
        return {date: len(memories) for date, memories in self.date_to_memories.items()}
    
    def save_index(self):
        """Save the date index to disk."""
        data = {
            "version": "0.4.0",
            "updated_at": datetime.now().isoformat(),
            "total_dates": len(self.date_to_memories),
            "total_memories": len(self.memory_to_date),
            "date_to_memories": dict(self.date_to_memories),
            "memory_to_date": self.memory_to_date
        }
        
        from .utils import atomic_write_json
        atomic_write_json(self.index_path, data)
    
    def load_index(self):
        """Load the date index from disk."""
        if not os.path.exists(self.index_path):
            return
        
        try:
            with open(self.index_path) as f:
                data = json.load(f)
            
            self.date_to_memories = defaultdict(list, data.get("date_to_memories", {}))
            self.memory_to_date = data.get("memory_to_date", {})
            
        except (json.JSONDecodeError, IOError):
            # Reset to empty state if index is corrupted
            self.date_to_memories = defaultdict(list)
            self.memory_to_date = {}


class IndexManager:
    """Manages all indexes for fast search."""
    
    def __init__(self, workspace: str):
        self.workspace = workspace
        
        self.search_index = SearchIndex(workspace)
        self.tag_index = TagIndex(workspace)
        self.date_index = DateIndex(workspace)
    
    def add_memory(self, memory: MemoryEntry):
        """Add a memory to all indexes."""
        self.search_index.add_memory(memory)
        self.tag_index.add_memory(memory)
        self.date_index.add_memory(memory)
    
    def remove_memory(self, memory_hash: str):
        """Remove a memory from all indexes."""
        self.search_index.remove_memory(memory_hash)
        self.tag_index.remove_memory(memory_hash)
        self.date_index.remove_memory(memory_hash)
    
    def search(self, 
               query: str,
               tags: Optional[List[str]] = None,
               tag_mode: str = "any",
               date_range: Optional[Tuple[str, str]] = None,
               limit: int = 50) -> List[Tuple[str, float]]:
        """Combined search across all indexes."""
        
        # Start with all candidate hashes
        candidate_hashes = None
        text_scores = {}
        
        # Get text search results if query is provided
        if query and query.strip():
            text_results = self.search_index.search(query, limit=limit*2)
            candidate_hashes = {hash for hash, score in text_results}
            text_scores = {hash: score for hash, score in text_results}
        
        # Filter by tags if specified
        if tags:
            tag_hashes = set(self.tag_index.get_memories_by_tags(tags, mode=tag_mode))
            if candidate_hashes is None:
                candidate_hashes = tag_hashes
            else:
                candidate_hashes &= tag_hashes
        
        # Filter by date range if specified
        if date_range:
            start_date, end_date = date_range
            date_hashes = set(self.date_index.get_memories_in_range(start_date, end_date))
            if candidate_hashes is None:
                candidate_hashes = date_hashes
            else:
                candidate_hashes &= date_hashes
        
        # If no filters were applied, return empty results
        if candidate_hashes is None:
            return []
        
        # Create results with scores (default score of 1.0 for non-text matches)
        results = []
        for hash in candidate_hashes:
            score = text_scores.get(hash, 1.0)  # Default score for tag/date only matches
            results.append((hash, score))
        
        # Sort by score and return
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:limit]
    
    def rebuild_indexes(self, memories: List[MemoryEntry]):
        """Rebuild all indexes from scratch."""
        # Clear existing indexes
        self.search_index.word_index.clear()
        self.search_index.term_frequencies.clear()
        self.search_index.memory_metadata.clear()
        
        self.tag_index.tag_to_memories.clear()
        self.tag_index.memory_to_tags.clear()
        
        self.date_index.date_to_memories.clear()
        self.date_index.memory_to_date.clear()
        
        # Add all memories
        for memory in memories:
            self.add_memory(memory)
    
    def save_all_indexes(self):
        """Save all indexes to disk."""
        self.search_index.save_index()
        self.tag_index.save_index()
        self.date_index.save_index()
    
    def get_combined_stats(self) -> Dict:
        """Get statistics from all indexes."""
        return {
            "search_index": self.search_index.get_stats(),
            "tag_stats": {
                "total_tags": len(self.tag_index.tag_to_memories),
                "total_memories": len(self.tag_index.memory_to_tags)
            },
            "date_stats": {
                "total_dates": len(self.date_index.date_to_memories),
                "date_distribution": self.date_index.get_date_distribution()
            }
        }