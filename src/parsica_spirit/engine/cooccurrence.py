"""
Local co-occurrence collection for the Parsica Spirit engine - additive semantic re-ranking.

Builds a per-user word-pair frequency index from stored memories.
No data upload, zero API calls, zero dependencies.

Every time a memory is stored, word pairs within a 5-word window are
recorded. Over time this builds a personalized semantic clustering index:
  - User talks about "liver function" -> index clusters "hepatic", "ALT", "enzymes"
  - Search for "liver" -> results containing "hepatic" get a boost

The index is stored as a simple JSON file alongside memories.
Completely independent of BM25 - it's an additive re-ranking layer.

Usage:
    from parsica_spirit.engine.cooccurrence import CooccurrenceIndex

    idx = CooccurrenceIndex("./workspace")
    idx.load()
    idx.update_from_memory("Patient shows elevated ALT and hepatic inflammation.")
    similar = idx.similar_words("liver", top_k=10)
    # -> ["hepatic", "ALT", "inflammation", "patient", ...]
    idx.save()
"""

import heapq
import json
import math
import os
import re
import threading
import time
import warnings
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# -- IndexedMinHeap -------------------------------------------------------------

class IndexedMinHeap:
    """Min-heap with O(log n) insert, update, delete, and pop-min.

    Maintains a position index so any item can be found and updated in
    O(log n) without scanning. Used for vocab eviction in the co-occurrence
    index - always know the least-recently-used word cheaply.

    Zero dependencies. Pure stdlib.

    Thread safety: NOT thread-safe. All callers must hold an external lock
    before calling any mutating method (push, pop_min, update, delete).
    CooccurrenceIndex enforces this via self._lock.
    """

    __slots__ = ("_heap", "_index")

    def __init__(self) -> None:
        self._heap: List[Optional[Tuple[float, str]]] = []  # (priority, key)
        self._index: Dict[str, int] = {}                    # key -> position in _heap

    def __len__(self) -> int:
        return len(self._heap)

    def __contains__(self, key: str) -> bool:
        return key in self._index

    # -- Mutation --------------------------------------------------------------

    def push(self, key: str, priority: float) -> None:
        """Insert a new key or update if it already exists. O(log n)."""
        if key in self._index:
            self.update(key, priority)
            return
        pos = len(self._heap)
        self._heap.append((priority, key))
        self._index[key] = pos
        self._sift_up(pos)

    def pop_min(self) -> Tuple[str, float]:
        """Remove and return (key, priority) with lowest priority. O(log n).

        Raises IndexError if empty.
        """
        if not self._heap:
            raise IndexError("pop from empty heap")
        self._swap(0, len(self._heap) - 1)
        priority, key = self._heap.pop()
        del self._index[key]
        if self._heap:
            self._sift_down(0)
        return key, priority

    def update(self, key: str, new_priority: float) -> None:
        """Change the priority of an existing key. O(log n).

        Raises KeyError if key not found.
        """
        if key not in self._index:
            raise KeyError(f"key not in heap: {key!r}")
        pos = self._index[key]
        old_priority = self._heap[pos][0]
        self._heap[pos] = (new_priority, key)
        if new_priority < old_priority:
            self._sift_up(pos)
        elif new_priority > old_priority:
            self._sift_down(pos)

    def delete(self, key: str) -> None:
        """Remove a key from the heap. O(log n).

        Raises KeyError if key not found.
        """
        if key not in self._index:
            raise KeyError(f"key not in heap: {key!r}")
        pos = self._index[key]
        last = len(self._heap) - 1
        if pos == last:
            self._heap.pop()
            del self._index[key]
            return
        self._swap(pos, last)
        self._heap.pop()
        del self._index[key]
        # After swapping the deleted element with the last element and removing
        # the last, the element now at `pos` (formerly the last element) may
        # need to move in either direction to restore heap order:
        #   - If it's smaller than its parent -> sift UP (it was a small element
        #     that happened to be at the end of the array)
        #   - Otherwise -> sift DOWN (it may be larger than its children)
        # Both cases must be checked. Only checking one direction is a common
        # heap implementation bug.
        if pos < len(self._heap):
            parent = (pos - 1) // 2
            if pos > 0 and self._heap[pos][0] < self._heap[parent][0]:
                self._sift_up(pos)
            else:
                self._sift_down(pos)

    # -- Query -----------------------------------------------------------------

    def peek_min(self) -> Tuple[str, float]:
        """Return (key, priority) of minimum element without removing. O(1).

        Raises IndexError if empty.
        """
        if not self._heap:
            raise IndexError("peek at empty heap")
        priority, key = self._heap[0]
        return key, priority

    def get_priority(self, key: str) -> float:
        """Get current priority for key. O(1). Raises KeyError if key not found."""
        if key not in self._index:
            raise KeyError(key)
        return self._heap[self._index[key]][0]

    # -- Internal --------------------------------------------------------------

    def _swap(self, i: int, j: int) -> None:
        self._heap[i], self._heap[j] = self._heap[j], self._heap[i]
        self._index[self._heap[i][1]] = i
        self._index[self._heap[j][1]] = j

    def _sift_up(self, pos: int) -> None:
        while pos > 0:
            parent = (pos - 1) // 2
            if self._heap[pos][0] < self._heap[parent][0]:
                self._swap(pos, parent)
                pos = parent
            else:
                break

    def _sift_down(self, pos: int) -> None:
        n = len(self._heap)
        while True:
            smallest = pos
            left = 2 * pos + 1
            right = 2 * pos + 2
            if left < n and self._heap[left][0] < self._heap[smallest][0]:
                smallest = left
            if right < n and self._heap[right][0] < self._heap[smallest][0]:
                smallest = right
            if smallest != pos:
                self._swap(pos, smallest)
                pos = smallest
            else:
                break


# Stopwords to exclude from co-occurrence pairs
_STOPWORDS = frozenset({
    'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
    'should', 'may', 'might', 'shall', 'can', 'to', 'of', 'in', 'for',
    'on', 'with', 'at', 'by', 'from', 'as', 'into', 'through', 'and',
    'or', 'but', 'if', 'that', 'this', 'it', 'its', 'he', 'she', 'they',
    'we', 'you', 'i', 'me', 'my', 'our', 'their', 'what', 'which', 'who',
    'not', 'no', 'so', 'then', 'just', 'now', 'up', 'out', 'very',
    'about', 'after', 'before', 'between', 'over', 'under', 'more',
})

# Window size for co-occurrence (words within N positions are considered co-occurring)
DEFAULT_WINDOW = 5
DEFAULT_TOP_K = 20
# Minimum time delta (seconds) before re-pushing a word to the LRU heap.
# Reduces O(log n) heap churn for high-frequency words in burst ingest:
# if a word was already pushed within the last second, skip the push.
_MIN_HEAP_UPDATE_INTERVAL = 1.0


class CooccurrenceIndex:
    """Per-user word-pair frequency index for personalized semantic search.

    Tracks which words appear near each other across all stored memories.
    Used to boost search results that contain words semantically related
    to the query terms - even when there's no exact keyword match.

    Args:
        workspace: Path to the memory workspace directory.
        window: Co-occurrence window size in words (default 5).
        filename: Index filename within workspace (default .cooccurrence.json).
    """

    def __init__(
        self,
        workspace: str,
        window: int = DEFAULT_WINDOW,
        filename: str = ".cooccurrence.json",
        vocab_cap: int = 50_000,
    ):
        self.workspace = Path(workspace)
        self.window = window
        self.vocab_cap = vocab_cap
        self._path = self.workspace / filename
        # word -> {co_word: count}
        self._counts: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        # word -> total occurrences (for PPMI normalization)
        self._word_counts: Dict[str, int] = defaultdict(int)
        # word -> last access time (for LRU eviction)
        self._access_times: Dict[str, float] = {}
        self._total_pairs: int = 0
        self._dirty: bool = False
        self._lock = threading.Lock()
        # Cache for pair-marginals used in PPMI scoring (invalidated on updates)
        self._pair_marginals: Dict[str, int] = {}
        self._pair_marginals_valid: bool = False
        # O(log n) LRU eviction heap (access_time as priority, lower = older)
        # NOT thread-safe - callers must hold an external lock (CooccurrenceIndex._lock).
        self._vocab_heap: IndexedMinHeap = IndexedMinHeap()

    # -- Load / Save -----------------------------------------------------------

    def load(self) -> None:
        """Load index from disk. No-op if index doesn't exist yet."""
        if not self._path.exists():
            return
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                data = json.load(f)
            raw_counts = data.get("counts", {})
            self._counts = defaultdict(lambda: defaultdict(int))
            for word, co_dict in raw_counts.items():
                self._counts[word] = defaultdict(int, co_dict)
            self._word_counts = defaultdict(int, data.get("word_counts", {}))
            self._access_times = data.get("access_times", {})
            # Migration: existing words without access times get a fair baseline
            if self._counts and not self._access_times:
                now = time.time()
                for word in self._counts:
                    self._access_times[word] = now
            self._total_pairs = data.get("total_pairs", 0)
        except (json.JSONDecodeError, KeyError):
            # Corrupted index - start fresh
            self._counts = defaultdict(lambda: defaultdict(int))
            self._word_counts = defaultdict(int)
            self._access_times = {}
            self._total_pairs = 0
        # rebuild O(log n) eviction heap from loaded access times
        # Use time.time() as fallback (not 0.0) so words with no recorded
        # access time are treated as recently seen rather than epoch-old,
        # which would cause them to be immediately evicted on next ingest.
        self._vocab_heap = IndexedMinHeap()
        now = time.time()
        for word in self._counts:
            t = self._access_times.get(word, now)
            self._vocab_heap.push(word, t)
        # Sanity check - should never fail since _counts is a plain dict (no duplicates).
        # If it does, something is wrong with the heap implementation itself.
        # A warning is better than silently retrying with identical code that produces
        # identical (still-mismatched) results.
        if len(self._vocab_heap) != len(self._counts):
            warnings.warn(
                f"[parsica-spirit engine] CooccurrenceIndex heap/counts mismatch after load "
                f"(heap={len(self._vocab_heap)}, counts={len(self._counts)}, "
                f"file={self._path or '<in-memory>'}). This indicates a bug in IndexedMinHeap.",
                RuntimeWarning,
                stacklevel=2,
            )
            # Accept the state - a warning is better than silently retrying and failing twice.
        self._dirty = False

    def save(self) -> None:
        """Save index to disk. No-op if nothing changed."""
        if not self._dirty:
            return
        self.workspace.mkdir(parents=True, exist_ok=True)
        data = {
            "counts": {w: dict(co) for w, co in self._counts.items()},
            "word_counts": dict(self._word_counts),
            "access_times": dict(self._access_times),
            "total_pairs": self._total_pairs,
            "version": "3.2",
        }
        # Atomic write with fsync for durability
        tmp = self._path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        tmp.replace(self._path)
        self._dirty = False

    def _evict_lru(self) -> int:
        """Evict least-recently-used words when vocab exceeds cap.

        O(log n) per eviction via IndexedMinHeap - replaces the old
        O(vocab_size * log k) heapq.nsmallest scan that caused quadratic ingest
        at large store sizes.

        Removes the oldest 10% of vocabulary by access time.
        Thread-safe: caller must hold self._lock or call from a locked context.
        Returns number of words evicted.
        """
        if self.vocab_size <= self.vocab_cap:
            return 0

        # If heap is empty but _counts is not (e.g. direct injection in
        # tests or migration from an older load path), rebuild the heap first.
        if not self._vocab_heap and self._counts:
            now = time.time()
            for word in self._counts:
                t = self._access_times.get(word, now)
                self._vocab_heap.push(word, t)

        # Evict 10% of current vocab, minimum 1
        evict_count = max(1, int(self.vocab_size * 0.1))
        max_ghost_pops = evict_count * 10  # safety cap: limits ghost-entry cleanup per call
                                            # does NOT bound stale re-insertions (see max_stale_pops)
        max_stale_pops = min(len(self._vocab_heap) * 2, evict_count * 50)  # safety cap: bounded by work being done, not total heap size
        pops = 0
        evicted = 0
        ghosts = 0
        stale_pops = 0

        while evicted < evict_count and len(self._vocab_heap) > 0 and pops < max_ghost_pops:
            word, heap_priority = self._vocab_heap.pop_min()

            # Stale-heap check: _access_times is always current even when heap push
            # was skipped by the churn guard. If the recorded access time is newer
            # than the popped heap priority, re-insert and retry - don't count this
            # against max_ghost_pops since the word is legitimately in the queue.
            actual_t = self._access_times.get(word, heap_priority)
            if actual_t > heap_priority:
                stale_pops += 1
                if stale_pops > max_stale_pops:
                    # Heap is massively stale - re-insert this word and abort eviction.
                    # This can happen when the churn guard has deferred many heap updates.
                    # The next call will see correct priorities after the re-insertions settle.
                    self._vocab_heap.push(word, actual_t)
                    warnings.warn(
                        f"[parsica-spirit engine] _evict_lru: too many stale heap entries "
                        f"(stale_pops={stale_pops}, max={max_stale_pops}). Aborting eviction. "
                        f"Heap priorities have been corrected; eviction will complete on next call. "
                        f"vocab_size={self.vocab_size}, heap_size={len(self._vocab_heap)}.",
                        RuntimeWarning,
                        stacklevel=3,
                    )
                    break
                self._vocab_heap.push(word, actual_t)
                continue  # NOT incrementing pops - this is a correction, not a pop

            pops += 1  # Only count words that are actually processed

            if word in self._counts:
                # Remove back-references from co-occurring words
                for co_word in list(self._counts[word].keys()):
                    if co_word in self._counts and word in self._counts[co_word]:
                        del self._counts[co_word][word]
                del self._counts[word]
                evicted += 1
            else:
                # Ghost word: in heap but not in counts (desync). Track for observability.
                # Don't increment evicted - ghost cleanup doesn't reduce vocab_size.
                # _word_counts and _access_times cleanup below still runs.
                ghosts += 1

            if word in self._word_counts:
                del self._word_counts[word]
            if word in self._access_times:
                del self._access_times[word]

        if evicted > 0:
            self._dirty = True
            self._pair_marginals_valid = False

        if ghosts > 0:
            # Ghost words were already evicted from _counts; their presence in the heap
            # is stale state, not a data change. Do NOT set _dirty - ghost cleanup
            # doesn't affect co-occurrence data, and a spurious dirty flag triggers
            # unnecessary recomputation.
            warnings.warn(
                f"[parsica-spirit engine] _evict_lru: cleaned up {ghosts} ghost heap "
                f"entries (words in heap but not in _counts). "
                f"vocab_size={self.vocab_size}, heap_size={len(self._vocab_heap)}.",
                RuntimeWarning,
                stacklevel=3,
            )

        return evicted

    # -- Indexing --------------------------------------------------------------

    def update_from_memory(self, content: str) -> int:
        """Extract word pairs from memory content and update the index.

        Processes a 5-word sliding window across the content, recording
        all non-stopword word pairs. Call this whenever a memory is stored.

        Args:
            content: Memory content string to index.

        Returns:
            Number of word pairs added.
        """
        tokens = self._tokenize(content)
        pairs_added = 0
        current_time = time.time()

        with self._lock:
            for i, word in enumerate(tokens):
                self._word_counts[word] += 1
                old_t = self._access_times.get(word, 0.0)
                self._access_times[word] = current_time
                # heap churn guard: only push if delta >= _MIN_HEAP_UPDATE_INTERVAL
                # to avoid O(log n) work per word per call during burst ingest.
                if current_time - old_t >= _MIN_HEAP_UPDATE_INTERVAL:
                    self._vocab_heap.push(word, current_time)

                window_end = min(i + self.window + 1, len(tokens))
                for j in range(i + 1, window_end):
                    co_word = tokens[j]
                    if co_word != word:
                        old_co_t = self._access_times.get(co_word, 0.0)
                        self._access_times[co_word] = current_time
                        # heap churn guard for co_word too
                        if current_time - old_co_t >= _MIN_HEAP_UPDATE_INTERVAL:
                            self._vocab_heap.push(co_word, current_time)
                        self._counts[word][co_word] += 1
                        self._counts[co_word][word] += 1
                        self._total_pairs += 2
                        pairs_added += 2

            if pairs_added:
                self._dirty = True
                self._pair_marginals_valid = False

                while self.vocab_size > self.vocab_cap:
                    evicted = self._evict_lru()
                    if evicted == 0:
                        if self.vocab_size > self.vocab_cap:
                            warnings.warn(
                                f"[parsica-spirit engine] update_from_memory: vocab eviction stalled "
                                f"(vocab_size={self.vocab_size}, vocab_cap={self.vocab_cap}). "
                                "This is transient during burst ingest - eviction resumes on next call.",
                                RuntimeWarning,
                                stacklevel=2,
                            )
                        break

        return pairs_added

    def update_from_entries(self, entries: list) -> int:
        """Rebuild index from a list of MemoryEntry objects.

        Args:
            entries: List of MemoryEntry objects.

        Returns:
            Total word pairs indexed.
        """
        total = 0
        for entry in entries:
            content = getattr(entry, "content", "")
            if content:
                total += self.update_from_memory(content)
        return total

    def _get_pair_marginal(self, word: str) -> int:
        """Return the marginal (row sum) for a word in co-occurrence-pair space."""
        if not self._pair_marginals_valid:
            self._pair_marginals = {w: sum(co.values()) for w, co in self._counts.items()}
            self._pair_marginals_valid = True
        return self._pair_marginals.get(word, 0)

    # -- Query -----------------------------------------------------------------

    def similar_words(self, word: str, top_k: int = DEFAULT_TOP_K) -> List[Tuple[str, float]]:
        """Find words most similar to the given word by co-occurrence.

        Uses Positive PMI (PPMI) scoring: penalizes common word pairs,
        rewards rare but meaningful co-occurrences.

        Args:
            word: Query word to find similar words for.
            top_k: Maximum number of results to return.

        Returns:
            List of (word, ppmi_score) tuples, sorted by score descending.
        """
        word = word.lower()
        if word not in self._counts or self._total_pairs == 0:
            return []

        with self._lock:
            # Update access time for queried word (thread-safe)
            t = time.time()
            old_t = self._access_times.get(word, 0.0)
            self._access_times[word] = t
            # heap churn guard: skip push if recently updated
            if t - old_t >= _MIN_HEAP_UPDATE_INTERVAL:
                self._vocab_heap.push(word, t)
            self._dirty = True

            co_counts = dict(self._counts[word])  # snapshot under lock
            total_pairs = self._total_pairs
            # Compute marginals in pair space (consistent denominator for PPMI)
            row_total = self._get_pair_marginal(word)
            if row_total <= 0:
                return []
            p_word = row_total / total_pairs

        scored = []
        for co_word, co_count in co_counts.items():
            col_total = self._get_pair_marginal(co_word)
            if col_total <= 0 or co_count <= 0:
                continue
            p_co = col_total / total_pairs
            p_joint = co_count / total_pairs

            if p_joint <= 0 or p_word <= 0 or p_co <= 0:
                continue

            # PMI = log(P(x,y) / (P(x) * P(y)))
            pmi = math.log2(p_joint / (p_word * p_co))
            # PPMI: clip negative values (reduces noise from rare pairs)
            ppmi = max(0.0, pmi)

            if ppmi > 0:
                scored.append((co_word, ppmi))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_k]

    def boost_score(
        self,
        query: str,
        entry_content: str,
        boost_factor: float = 0.3,
    ) -> float:
        """Compute a semantic boost score for an entry given a query.

        Checks whether any query terms co-occur strongly with terms in the
        entry content. Returns an additive boost (0.0 to boost_factor).

        Args:
            query: The search query string.
            entry_content: The memory entry content to check.
            boost_factor: Maximum boost to apply (default 0.3 = 30% extra).

        Returns:
            Float boost value in [0.0, boost_factor].
        """
        query_tokens = self._tokenize(query)
        entry_tokens = set(self._tokenize(entry_content))

        if not query_tokens or not entry_tokens:
            return 0.0

        # Update access times for query tokens (thread-safe)
        current_time = time.time()
        with self._lock:
            for token in query_tokens:
                if token in self._counts:
                    old_t = self._access_times.get(token, 0.0)
                    self._access_times[token] = current_time
                    # heap churn guard: skip push if recently updated
                    if current_time - old_t >= _MIN_HEAP_UPDATE_INTERVAL:
                        self._vocab_heap.push(token, current_time)
                    self._dirty = True

        total_boost = 0.0
        hits = 0
        cache = {}

        for q_token in query_tokens:
            if q_token not in cache:
                cache[q_token] = dict(self.similar_words(q_token, top_k=20))
            similar = cache[q_token]
            for e_token in entry_tokens:
                if e_token in similar:
                    # Scale similarity score to [0, 1] range (PPMI can be large)
                    raw_score = similar[e_token]
                    scaled = min(raw_score / 5.0, 1.0)  # Cap at PPMI=5
                    total_boost += scaled
                    hits += 1

        if hits == 0:
            return 0.0

        # Average boost, scaled to boost_factor
        avg = total_boost / hits
        return min(avg * boost_factor, boost_factor)

    # -- Stats -----------------------------------------------------------------

    @property
    def vocab_size(self) -> int:
        """Number of unique words in the index."""
        return len(self._counts)

    @property
    def total_pairs(self) -> int:
        """Total co-occurrence pairs recorded."""
        return self._total_pairs

    def stats(self) -> Dict:
        """Return index statistics."""
        return {
            "vocab_size": self.vocab_size,
            "total_pairs": self._total_pairs,
            "top_words": sorted(
                self._word_counts.items(), key=lambda x: x[1], reverse=True
            )[:10],
        }

    # -- Internal --------------------------------------------------------------

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """Extract meaningful tokens from text, excluding stopwords.

        Captures alphanumeric tokens (>=2 chars) so that domain-specific
        codes like "404", "gpt4", "100mg", and version strings are indexed.
        """
        tokens = re.findall(r'\b[a-zA-Z0-9]{2,}\b', text.lower())
        return [t for t in tokens if t not in _STOPWORDS]
