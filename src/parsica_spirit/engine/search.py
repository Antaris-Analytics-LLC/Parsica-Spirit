"""
Search engine for the Parsica Spirit engine.

Provides ranked search with BM25-inspired scoring, combining:
- Layer 1: Term frequency / inverse document frequency (BM25+ variant)
- Layer 2: Exact phrase bonus
- Layer 3: Field boosting (tags, source, category)
- Layer 4: Rarity boost + proper noun boost
- Layer 5: Sliding window context (with positional salience)
- Layer 6: Semantic (CategoryTagger)
- Layer 7: Intent reranker (post-scoring)
- Layer 8: Qualifier & negation sensitivity (post-scoring)
- Layer 9: Cross-memory clustering (post-scoring)
- Layer 10: Pseudo-Relevance Feedback
  Top-term extraction from top-3 BM25 results. Expands query with 3 high-IDF
  terms, re-scores top-50 candidates. +3-8ms overhead.
- Layer 11: Post-scoring adjustments (anti-adjacency penalty; recency boost for
  competing versions of the same fact)

All deterministic, zero dependencies.
"""

import logging
import math
import os
import re
from collections import Counter
from typing import List, Dict, Optional, Tuple, Set
from dataclasses import dataclass
from .category_tagger import CategoryTagger

# Module-level singleton to avoid re-instantiating per search call
_category_tagger = CategoryTagger()

from .windowing import WindowIndexer
from .clustering import ClusterIndex

logger = logging.getLogger(__name__)


# ===========================================================================
# BM25+ delta constant
# ===========================================================================
# BM25+ adds a floor delta to the TF component, preventing near-zero scores on
# legitimate partial matches (terms that appear once in a long doc). This
# lifts all scores uniformly without changing relative ordering of entries
# that share the same TF pattern, and recovers candidates that BM25 scores
# near zero due to document length normalization.
_BM25_DELTA: float = 1.0  # standard recommendation; Lv & Zhai 2011

# ===========================================================================
# Safelist normalizer - domain-specific morphological normalization
# ===========================================================================
# Applied at both index-build time and query time. Zero false positives
# (whitelist approach). Covers the morphological variants most common in
# agent conversation stores that Porter stemmer either misses or damages.
_NORM_MAP: Dict[str, str] = {
    # memory family
    "memories": "memory", "memorize": "memory", "memorized": "memory",
    "memorizing": "memory", "memorizes": "memory",
    # retrieve family
    "retrieval": "retrieve", "retrieved": "retrieve",
    "retrieving": "retrieve", "retrieves": "retrieve",
    # search family
    "searches": "search", "searched": "search", "searching": "search",
    # store family
    "stores": "store", "stored": "store", "storing": "store",
    "storage": "store",
    # deploy family (Porter fails this - two stems for deploy/deployment)
    "deploys": "deploy", "deployed": "deploy", "deploying": "deploy",
    "deployment": "deploy", "deployments": "deploy",
    # configure family
    "configures": "configure", "configured": "configure",
    "configuring": "configure", "configuration": "configure",
    "configurations": "configure",
    # optimize family
    "optimizes": "optimize", "optimized": "optimize",
    "optimizing": "optimize", "optimization": "optimize",
    # authenticate family
    "authenticates": "authenticate", "authenticated": "authenticate",
    "authenticating": "authenticate", "authentication": "authenticate",
    # generate family (NOT general/generic - only true generate variants)
    "generates": "generate", "generated": "generate",
    "generating": "generate", "generation": "generate",
    # enrich family
    "enriches": "enrich", "enriched": "enrich", "enriching": "enrich",
    "enrichment": "enrich",
    # index family
    "indexes": "index", "indexed": "index", "indexing": "index",
    # ingest family
    "ingests": "ingest", "ingested": "ingest", "ingesting": "ingest",
    "ingestion": "ingest",
    # compress family
    "compresses": "compress", "compressed": "compress",
    "compressing": "compress", "compression": "compress",
    # shard family
    "shards": "shard", "sharded": "shard", "sharding": "shard",
    # embed family
    "embeds": "embed", "embedded": "embed", "embedding": "embed",
    "embeddings": "embedding",
}

def _norm(token: str) -> str:
    """Apply safelist normalization to a single token."""
    return _NORM_MAP.get(token, token)


# ===========================================================================
# Layer 7 - Intent Reranker
# ===========================================================================

class IntentReranker:
    """Classify query intent and re-score candidates by structural match.

    Intents:
        temporal    - "when", "what time", "how long ago", "date of"
        entity      - "who", "which person", "whose", "name of"
        event       - "what happened", "tell me about", "describe"
        decision    - "did we decide", "what was decided", "choice", "agreed"
        comparison  - "vs", "compare", "difference between", "better"
        howto       - "how to", "how do", "steps to", "way to"
        quantity    - "how much", "how many", "count", "total", "amount"
        location    - "where", "which place", "location of"

    Each intent has associated memory-side signals that get boosted when found.
    """

    # Query-side intent detection patterns
    INTENT_PATTERNS = {
        "temporal": [
            re.compile(r'\b(when|what\s+time|how\s+long\s+ago|date\s+of|what\s+date|timeline|since\s+when)\b', re.I),
            re.compile(r'\b(before|after|during|yesterday|last\s+week|last\s+month|recently)\b', re.I),
        ],
        "entity": [
            re.compile(r'\b(who|whose|which\s+person|name\s+of|who\'s|whom)\b', re.I),
        ],
        "event": [
            re.compile(r'\b(what\s+happened|tell\s+me\s+about|describe|incident|event|occurred|took\s+place)\b', re.I),
        ],
        "decision": [
            re.compile(r'\b(decid|decision|agreed|chose|choice|settled\s+on|went\s+with|conclusion|resolved\s+to)\b', re.I),
        ],
        "comparison": [
            re.compile(r'\b(vs\.?|versus|compar|difference\s+between|better\s+than|worse\s+than|pros?\s+and\s+cons?)\b', re.I),
        ],
        "howto": [
            re.compile(r'\b(how\s+to|how\s+do|how\s+can|steps?\s+to|way\s+to|process\s+for|instructions?)\b', re.I),
        ],
        "quantity": [
            re.compile(r'\b(how\s+much|how\s+many|count|total|amount|number\s+of|sum\s+of)\b', re.I),
        ],
        "location": [
            re.compile(r'\b(where|which\s+place|location\s+of|located)\b', re.I),
        ],
    }

    # Memory-side signals that indicate a memory satisfies an intent
    MEMORY_SIGNALS = {
        "temporal": [
            # ISO dates, relative dates, timestamps
            re.compile(r'\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b'),
            re.compile(r'\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2}\b', re.I),
            re.compile(r'\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b', re.I),
            re.compile(r'\b(?:yesterday|today|last\s+week|last\s+month|ago|since)\b', re.I),
            re.compile(r'\b\d{1,2}:\d{2}\b'),  # time patterns
        ],
        "entity": [
            # Capitalized names (rough proxy for named entities)
            re.compile(r'\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+\b'),
            re.compile(r'\b(?:@\w+)\b'),  # @mentions
        ],
        "event": [
            re.compile(r'\b(?:happened|occurred|incident|outage|launched|shipped|broke|failed|succeeded|completed)\b', re.I),
        ],
        "decision": [
            re.compile(r'\b(?:decided|agreed|chose|picked|selected|went\s+with|settled|concluded|resolution)\b', re.I),
        ],
        "comparison": [
            re.compile(r'\b(?:vs\.?|versus|compared|better|worse|faster|slower|cheaper|pros?|cons?)\b', re.I),
        ],
        "howto": [
            re.compile(r'\b(?:step\s+\d|first|then|next|finally|instructions?|guide|tutorial|procedure)\b', re.I),
        ],
        "quantity": [
            re.compile(r'\$[\d,.]+|\b\d+[kmb]?\b', re.I),
            re.compile(r'\b(?:total|sum|count|amount|percent|ratio)\b', re.I),
        ],
        "location": [
            re.compile(r'\b(?:office|building|room|floor|city|country|region|location|address|remote|onsite)\b', re.I),
        ],
    }

    # Boost applied when a memory matches the detected intent
    INTENT_BOOST = 1.25
    # Penalty applied when a memory contradicts the detected intent
    # (e.g., query asks "when" but memory has zero temporal signals)
    INTENT_PENALTY = 0.85

    @classmethod
    def detect_intent(cls, query: str) -> List[str]:
        """Detect intent(s) from the query. Returns list of intent names.

        A query can match multiple intents (e.g., "when did we decide" ->
        ["temporal", "decision"]).
        """
        intents = []
        for intent_name, patterns in cls.INTENT_PATTERNS.items():
            for pattern in patterns:
                if pattern.search(query):
                    intents.append(intent_name)
                    break  # one match per intent is enough
        return intents

    @classmethod
    def score_intent_match(cls, content: str, intents: List[str]) -> float:
        """Score how well a memory's content matches the detected intents.

        Returns a multiplier: >1.0 = boost, <1.0 = penalty, 1.0 = neutral.
        """
        if not intents:
            return 1.0

        matches = 0
        for intent in intents:
            signals = cls.MEMORY_SIGNALS.get(intent, [])
            for signal in signals:
                if signal.search(content):
                    matches += 1
                    break  # one signal match per intent is enough

        if matches == len(intents):
            # Memory satisfies all detected intents
            return cls.INTENT_BOOST
        elif matches > 0:
            # Partial match - mild boost
            return 1.0 + (cls.INTENT_BOOST - 1.0) * (matches / len(intents))
        else:
            # No intent signals found - mild penalty
            return cls.INTENT_PENALTY

    @classmethod
    def rerank(cls, query: str, results: list) -> list:
        """Rerank results based on intent match.

        Parameters
        ----------
        query : str
            Original search query.
        results : list
            List of (entry, score, matched_terms) tuples.

        Returns
        -------
        list
            Same tuples with adjusted scores, re-sorted.
        """
        intents = cls.detect_intent(query)
        if not intents:
            return results  # no intent detected, skip

        reranked = []
        for item in results:
            entry, score, matched = item[0], item[1], item[2]
            rest = item[3:] if len(item) > 3 else ()
            multiplier = cls.score_intent_match(entry.content, intents)
            new_score = score * multiplier
            reranked.append((entry, new_score, matched, *rest))

        reranked.sort(key=lambda x: x[1], reverse=True)
        return reranked


# ===========================================================================
# Layer 8 - Qualifier & Negation Sensitivity
# ===========================================================================

class QualifierAnalyzer:
    """Analyze temporal, outcome, negation, and degree qualifiers.

    Detects qualifiers in both queries and memories, then computes an
    alignment score: matched qualifiers boost, mismatched penalize.
    """

    # Qualifier categories with their antonyms / conflict groups
    TEMPORAL_BEFORE = frozenset({
        'before', 'prior', 'previously', 'earlier', 'preceding',
        'pre', 'until', 'up_to', 'leading_up',
    })
    TEMPORAL_AFTER = frozenset({
        'after', 'following', 'subsequently', 'later', 'since',
        'post', 'once', 'afterward',
    })
    TEMPORAL_DURING = frozenset({
        'during', 'while', 'meanwhile', 'concurrent', 'simultaneously',
        'at_the_time', 'in_progress',
    })

    OUTCOME_SUCCESS = frozenset({
        'succeeded', 'successful', 'passed', 'completed', 'resolved',
        'fixed', 'shipped', 'launched', 'achieved', 'won', 'approved',
        'merged', 'deployed', 'delivered', 'working', 'green',
    })
    OUTCOME_FAILURE = frozenset({
        'failed', 'failure', 'broke', 'broken', 'crashed', 'rejected',
        'denied', 'cancelled', 'abandoned', 'reverted', 'rolled_back',
        'missed', 'lost', 'red', 'blocked', 'errored',
    })

    NEGATION = frozenset({
        'not', 'never', 'no', 'without', 'none', "didn't", "doesn't",
        "wasn't", "weren't", "isn't", "aren't", "don't", "won't",
        "can't", "couldn't", "shouldn't", "wouldn't", "hasn't", "haven't",
        'neither', 'nor', 'nothing', 'nowhere',
    })

    DEGREE_MOST = frozenset({
        'most', 'best', 'highest', 'maximum', 'top', 'greatest',
        'largest', 'biggest', 'primary', 'main', 'first',
    })
    DEGREE_LEAST = frozenset({
        'least', 'worst', 'lowest', 'minimum', 'bottom', 'smallest',
        'fewest', 'last', 'final', 'minor',
    })

    # Scoring constants
    MATCH_BOOST = 1.15       # qualifier alignment bonus
    MISMATCH_PENALTY = 0.80  # qualifier conflict penalty
    NEGATION_PENALTY = 0.75  # negation mismatch penalty

    @classmethod
    def _extract_qualifiers(cls, text: str) -> Dict[str, str]:
        """Extract qualifier categories from text.

        Returns dict of {category: direction}, e.g.:
            {"temporal": "before", "outcome": "failure", "negation": "present"}
        """
        # Normalize contractions and tokenize
        text_lower = text.lower()
        # Handle contractions for negation detection
        text_norm = text_lower.replace("'", "'")
        tokens = set(re.findall(r"[\w']+", text_norm))

        qualifiers = {}

        # Temporal
        if tokens & cls.TEMPORAL_BEFORE:
            qualifiers["temporal"] = "before"
        elif tokens & cls.TEMPORAL_AFTER:
            qualifiers["temporal"] = "after"
        elif tokens & cls.TEMPORAL_DURING:
            qualifiers["temporal"] = "during"

        # Outcome
        if tokens & cls.OUTCOME_SUCCESS:
            qualifiers["outcome"] = "success"
        if tokens & cls.OUTCOME_FAILURE:
            # If both present, mark as "mixed" - no penalty either way
            if qualifiers.get("outcome") == "success":
                qualifiers["outcome"] = "mixed"
            else:
                qualifiers["outcome"] = "failure"

        # Negation
        if tokens & cls.NEGATION:
            qualifiers["negation"] = "present"

        # Degree
        if tokens & cls.DEGREE_MOST:
            qualifiers["degree"] = "most"
        elif tokens & cls.DEGREE_LEAST:
            qualifiers["degree"] = "least"

        return qualifiers

    @classmethod
    def compute_alignment(cls, query: str, content: str) -> float:
        """Compute qualifier alignment between query and memory content.

        Returns a multiplier: >1.0 = aligned, <1.0 = conflicting, 1.0 = neutral.
        """
        q_quals = cls._extract_qualifiers(query)
        m_quals = cls._extract_qualifiers(content)

        if not q_quals:
            return 1.0  # no qualifiers in query, nothing to check

        multiplier = 1.0

        for category, q_direction in q_quals.items():
            m_direction = m_quals.get(category)

            if category == "negation":
                # If query has negation but memory doesn't (or vice versa),
                # this is a likely mismatch
                if q_direction == "present" and m_direction != "present":
                    # Query says "not X" - memory that says "X" is probably wrong
                    multiplier *= cls.NEGATION_PENALTY
                continue

            if m_direction is None:
                # Memory has no signal for this qualifier - neutral
                continue

            if m_direction == "mixed":
                # Memory has both signals - neutral
                continue

            if q_direction == m_direction:
                # Aligned - boost
                multiplier *= cls.MATCH_BOOST
            else:
                # Conflicting - penalize
                multiplier *= cls.MISMATCH_PENALTY

        return multiplier

    @classmethod
    def apply(cls, query: str, results: list) -> list:
        """Apply qualifier sensitivity to search results.

        Parameters
        ----------
        query : str
            Original search query.
        results : list
            List of (entry, score, matched_terms) tuples.

        Returns
        -------
        list
            Same tuples with adjusted scores, re-sorted.
        """
        # Quick check: does the query even have qualifiers?
        q_quals = cls._extract_qualifiers(query)
        if not q_quals:
            return results  # no qualifiers, skip entirely

        adjusted = []
        for item in results:
            entry, score, matched = item[0], item[1], item[2]
            rest = item[3:] if len(item) > 3 else ()
            multiplier = cls.compute_alignment(query, entry.content)
            new_score = score * multiplier
            adjusted.append((entry, new_score, matched, *rest))

        adjusted.sort(key=lambda x: x[1], reverse=True)
        return adjusted


# ===========================================================================
# SearchResult + SearchEngine
# ===========================================================================

@dataclass
class SearchResult:
    """A search result with relevance score and explanation."""
    entry: object  # MemoryEntry
    score: float
    relevance: float  # 0.0-1.0 normalized
    matched_terms: List[str]
    explanation: str
    _breakdown: Optional[dict] = None  # per-component score breakdown
    
    @property
    def content(self):
        return self.entry.content
    
    @property
    def source(self):
        return self.entry.source
    
    @property
    def confidence(self):
        return self.relevance
    
    @property
    def category(self):
        return self.entry.category
    
    def __repr__(self):
        return f"<SearchResult score={self.score:.3f} relevance={self.relevance:.2f} '{self.content[:50]}...'>"


class SearchEngine:
    """BM25-inspired search engine for memory entries.
    
    Computes relevance using term frequency, inverse document frequency,
    field boosting, and decay weighting. Deterministic for a fixed store,
    configuration, and reference time; uses only the Python standard library.

    additions:
    - Layer 7: Intent reranker (post-scoring)
    - Layer 8: Qualifier & negation sensitivity (post-scoring)
    - Layer 9: Cross-memory clustering (post-scoring, via ClusterIndex)
    - Layer 5 enhancement: Positional salience (first/last window boost)
    - All new layers have bypass flags (use_layer7, use_layer8, use_layer9)
    
    Args:
        k1: Term frequency saturation parameter (default 1.5)
        b: Length normalization parameter (default 0.75)
    """
    
    # Stopwords to exclude from scoring (common English words)
    STOPWORDS = frozenset({
        'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
        'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
        'should', 'may', 'might', 'shall', 'can', 'need', 'dare', 'ought',
        'used', 'to', 'of', 'in', 'for', 'on', 'with', 'at', 'by', 'from',
        'as', 'into', 'through', 'during', 'before', 'after', 'above', 'below',
        'between', 'out', 'off', 'over', 'under', 'again', 'further', 'then',
        'once', 'here', 'there', 'when', 'where', 'why', 'how', 'all', 'both',
        'each', 'few', 'more', 'most', 'other', 'some', 'such', 'no', 'nor',
        'not', 'only', 'own', 'same', 'so', 'than', 'too', 'very', 'just',
        'don', 'now', 'and', 'but', 'or', 'if', 'while', 'that', 'this',
        'it', 'its', 'he', 'she', 'they', 'them', 'his', 'her', 'their',
        'what', 'which', 'who', 'whom', 'these', 'those', 'am', 'about',
        'up', 'down', 'we', 'our', 'you', 'your', 'my', 'me', 'i',
    })
    
    # Pre-compiled regex patterns for performance (used in _anti_adjacency_penalty and _specific_identifier_boost)
    _RE_PROPER_NOUN = re.compile(r'[A-Z][a-z]+(?:\s+[A-Z][a-z]+)*')
    _RE_QUOTED_STRING = re.compile(r'"[^"]{2,}"')
    _RE_VERSION_NUMBER = re.compile(r'\bv?\d+\.\d+(?:\.\d+)?\b')
    _RE_CODE_IDENTIFIER = re.compile(r'`[^`]+`')
    _RE_CONSTANT = re.compile(r'\b[A-Z][A-Z_]{2,}\b')
    
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._idf_cache: Dict[str, float] = {}
        self._avg_doc_len: float = 0
        self._doc_count: int = 0
        self._doc_freqs: Counter = Counter()  # term -> number of docs containing it
        self._corpus_version: int = 0
        self._indexed_version: int = -1
        # Content fingerprint: lightweight hash of all memory content at index time.
        # Detects in-place content mutations that don't change the memory count
        # (e.g. tag updates, content edits) - fixes search index drift.
        self._content_fingerprint: int = 0
        # Cross-memory clustering (Layer 9)
        self._cluster_index = ClusterIndex()
        # Cached sliding windows (Layer 5) - rebuilt in build_index(), not per-search.
        # Building windows is O(n) and was previously re-run on every single search call.
        self._cached_windows: list = []
        self._window_size: int = 3
        self._window_stride: int = 1

    @staticmethod
    def _compute_fingerprint(memories: list) -> int:
        """XOR hash of all memory content hashes - cheap O(n) mutation detector."""
        fp = 0
        for m in memories:
            fp ^= hash(m.content)
        return fp

    def build_index(self, memories: list) -> None:
        """Build IDF statistics from the memory corpus.
        
        Call this after loading/ingesting memories to enable proper ranking.
        Also builds cluster index for Layer 9.
        """
        self._indexed_version = self._corpus_version
        self._content_fingerprint = self._compute_fingerprint(memories)
        self._doc_count = len(memories)
        self._doc_freqs.clear()
        self._idf_cache.clear()
        self._token_cache: Dict[str, Tuple[List[str], List[str], List[str], Counter]] = {}
        self._inverted_index: Dict[str, Set[str]] = {}  # token -> set of memory hashes
        
        total_len = 0
        for mem in memories:
            tokens = self._tokenize(mem.content)

            # Cache tokenization for _score_entry() reuse
            enriched_tokens = self._tokenize(mem.enriched_summary) if getattr(mem, "enriched_summary", "") else []
            keyword_tokens = []
            if getattr(mem, "search_keywords", []):
                for kw in mem.search_keywords:
                    keyword_tokens.extend(self._tokenize(str(kw)))
            # Fix: include search_queries tokens so doc2query terms are findable.
            sq_tokens = []
            for sq in (getattr(mem, "search_queries", []) or []):
                sq_tokens.extend(self._tokenize(str(sq)))

            # Field-weighted all_tokens: search_queries 3x, enriched_summary 2x,
            # content and search_keywords 1x. Token repetition raises TF for
            # high-signal fields without changing IDF (set() deduplication in
            # _doc_freqs ensures IDF counts each document once per term).
            # avg_doc_len scales proportionally across all docs so BM25
            # length normalisation remains unbiased.
            all_tokens = (
                tokens
                + enriched_tokens + enriched_tokens          # 2x enriched_summary
                + keyword_tokens + keyword_tokens            # 2x search_keywords (was 1x)
                + sq_tokens + sq_tokens + sq_tokens          # 3x search_queries
            )
            e_lower = mem.enriched_summary.lower() if getattr(mem, "enriched_summary", "") else ""
            self._token_cache[mem.hash] = (tokens, enriched_tokens, all_tokens, Counter(all_tokens), e_lower)

            # doc_freqs uses set(all_tokens) so repetition doesn't skew IDF.
            # avg_doc_len uses len(all_tokens) which is fine: all docs are
            # weighted consistently, keeping the ratio doc_len/avg_doc_len stable.
            total_len += len(all_tokens)
            for term in set(all_tokens):
                self._doc_freqs[term] += 1

            # Build inverted index: for each unique token, add memory hash
            for token in set(all_tokens):
                if token not in self._inverted_index:
                    self._inverted_index[token] = set()
                self._inverted_index[token].add(mem.hash)

        self._avg_doc_len = total_len / max(self._doc_count, 1)
        
        # Pre-compute IDF for all terms
        for term, df in self._doc_freqs.items():
            # BM25 IDF formula with smoothing
            self._idf_cache[term] = math.log(
                (self._doc_count - df + 0.5) / (df + 0.5) + 1.0
            )

        # Build sliding window cache (Layer 5) here, not per-search.
        # WindowIndexer.build_windows() is O(n) - acceptable at index time, not query time.
        if len(memories) >= 2:
            try:
                _win_indexer = WindowIndexer(
                    window_size=self._window_size,
                    stride=self._window_stride,
                )
                self._cached_windows = _win_indexer.build_windows(memories)
            except Exception:
                self._cached_windows = []
        else:
            self._cached_windows = []

        # NOTE: Cluster index (Layer 9) is intentionally NOT rebuilt here.
        # build_index() is called on every search when the corpus changes, making
        # cluster rebuild O(nxe) per search - unacceptably slow at scale.
        # Call rebuild_clusters() explicitly after bulk ingestion or via flush().

    def rebuild_clusters(self, memories: list) -> None:
        """Rebuild the Layer 9 cluster index from the given memory corpus.

        Decoupled from build_index() intentionally - cluster building is O(nxe)
        and should run at write time (flush/bulk-ingest), not on every search.

        Called automatically by MemorySystem.flush() and rebuild_clusters().
        """
        try:
            self._cluster_index.build(memories)
        except Exception:
            pass  # Non-fatal - Layer 9 boost is additive, search works without it

    def _rarity_boost(self, term: str) -> float:
        """Return a boost multiplier based on how rarely a term appears across documents.

        Enhanced tiers:
          - <= 1%  of docs  -> 3.0  (extremely rare / highly specific - was 2.0)
          - 1-5%  of docs  -> 2.25  (very rare - was 1.5) 
          - 5-15% of docs  -> 1.2   (uncommon - unchanged)
          - >15%  of docs  -> 1.0   (common, no boost)

        Falls back to 2.0 when the index is empty or the term is unknown.
        """
        if self._doc_count == 0:
            # Return 2.0 instead of 1.0 for unindexed terms. Before the search
            # index is built (first search), we assume terms are potentially rare rather
            # than neutral. In practice build_index() runs before search, so this only
            # affects the edge case of searching before any index exists.
            return 2.0
        df = self._doc_freqs.get(term, 0)
        if df == 0:
            # Term not in corpus at all - treat as maximally rare
            return 3.0
        ratio = df / self._doc_count
        if ratio <= 0.01:
            return 3.0  # Enhanced boost for extremely rare terms
        elif ratio <= 0.05:
            return 2.25  # Enhanced boost for very rare terms (was 1.5)
        elif ratio <= 0.15:
            return 1.2
        else:
            return 1.0

    @staticmethod
    def _is_proper_noun(term: str, capitalized_terms: set) -> bool:
        """Heuristic: returns True if *term* was written in Title-case in the query."""
        return term in capitalized_terms
    
    def _specific_identifier_boost(self, term: str, content: str) -> float:
        """Return boost multiplier for specific identifiers (quoted strings, version numbers, etc.).
        
        Enhanced boosts:
        - Quoted strings: 3.0x
        - Version numbers: 2.5x
        - Code identifiers (backticks): 2.5x
        - CONSTANTS/ACRONYMS: 2.0x
        """
        # Check for quoted strings containing this term (dynamic pattern - can't pre-compile)
        quoted_pattern = rf'"[^"]*\b{re.escape(term)}\b[^"]*"'
        if re.search(quoted_pattern, content, re.IGNORECASE):
            return 3.0
        
        # Check for version numbers (using pre-compiled pattern)
        if self._RE_VERSION_NUMBER.search(term):
            return 2.5
            
        # Check for code identifiers in backticks (dynamic pattern - can't pre-compile)
        code_pattern = rf'`[^`]*\b{re.escape(term)}\b[^`]*`'
        if re.search(code_pattern, content, re.IGNORECASE):
            return 2.5
            
        # Check for CONSTANTS/ACRONYMS (all caps, 2+ chars)
        if len(term) >= 2 and term.isupper() and term.isalpha():
            return 2.0
            
        return 1.0
    
    def _anti_adjacency_penalty(self, query: str, content: str) -> float:
        """Penalize results that match topic but not the specific answer.
        
        Layer 11: Anti-Adjacency Penalty
        
        Returns a multiplier: 1.0 (no penalty) or 0.6-0.85 (penalized).
        Only applies when query asks for something specific.
        """
        query_lower = query.lower()
        content_lower = content.lower()
        
        # Only apply when query asks for something specific
        question_patterns = [
            "what is ", "what was ", "what's ", "who is ", "who was ",
            "which ", "name of ", "called ", "what did we ",
            "what do ", "what does ", "how do ", "how does ",
        ]
        is_specific_query = any(query_lower.startswith(p) or f" {p}" in query_lower for p in question_patterns)
        if not is_specific_query:
            return 1.0  # No penalty for general queries
        
        # Check if content contains specific identifiers (proper nouns, quoted values, numbers, code)
        has_specific_value = bool(
            self._RE_PROPER_NOUN.search(content) or  # Proper nouns
            self._RE_QUOTED_STRING.search(content) or  # Quoted strings
            self._RE_VERSION_NUMBER.search(content) or  # Version numbers
            self._RE_CODE_IDENTIFIER.search(content) or  # Code/identifiers
            self._RE_CONSTANT.search(content)  # CONSTANTS/ACRONYMS
        )
        
        # Check if content is meta-discussion about the topic rather than containing the answer
        meta_patterns = [
            "we talked about", "we discussed", "memory test",
            "was mentioned", "came up in", "related to",
            "we were working on", "in the context of",
        ]
        is_meta = any(p in content_lower for p in meta_patterns)
        
        if is_meta and not has_specific_value:
            return 0.6  # Strong penalty: meta-discussion without concrete answer
        elif not has_specific_value:
            return 0.85  # Mild penalty: no specific value found
        
        return 1.0  # Has specific content, no penalty

    def search(self, query: str, memories: list, limit: int = 20,
               category: str = None, min_score: float = 0.01,
               decay_fn=None, session_id: Optional[str] = None,
               use_windows: bool = True,
               window_size: int = 3, window_stride: int = 1,
               agent_id: Optional[str] = None,
               # New layer toggles
               use_layer7: bool = True,
               use_layer8: bool = True,
               use_layer9: bool = True,
               positional_boost: float = 1.3,
               index_manager=None,
               ) -> List[SearchResult]:
        """Search memories with BM25-inspired ranking.

        Args:
            query: Search query string
            memories: List of MemoryEntry objects to search
            limit: Maximum results to return
            category: Filter by category (optional)
            min_score: Minimum relevance score threshold
            decay_fn: Optional decay scoring function (entry -> float)
            session_id: Session isolation filter.
                - None (default): defaults to '*' for backward compat.
                - '*': Return memories across all sessions.
                - <string>: Return only memories belonging to that session.
            use_windows: Enable/disable Layer 5 sliding window scoring.
            window_size: Number of entries per window (Layer 5).
            window_stride: Step size between windows (Layer 5).
            agent_id: Filter by agent_id.
            use_layer7: Enable/disable Layer 7 intent reranker.
            use_layer8: Enable/disable Layer 8 qualifier sensitivity.
            use_layer9: Enable/disable Layer 9 cross-memory clustering.
            positional_boost: First/last window boost factor (default 1.3).

        Returns:
            List of SearchResult objects, sorted by relevance descending.
        """
        # -- session_id guard (Issue #7) --------------------------------------
        # session_id=None -> defaults to '*' (cross-session wildcard) for backward
        # compatibility. Callers that need session isolation should pass an
        # explicit session_id string. The MemorySystem.search() API defaults to
        # session_id='*' so None only reaches here from direct SearchEngine use.
        if session_id is None:
            session_id = '*'

        # -- Semantic / query preprocessing --------------------------------
        original_query = query  # preserve for intent detection
        # Vocabulary bridging happens on the DOCUMENT side: enrichment fields
        # (search_keywords 2x, enriched_summary 2x, search_queries 3x) are
        # weighted into the index at write time, so a short query stays sharp
        # against the inverted-index pre-filter and BM25 discrimination.

        # -- proper-noun detection (Layer 4) ---------------------------------
        raw_words = query.split()
        capitalized_terms: set = set()
        for word in raw_words[1:]:           # skip first word
            stripped = re.sub(r'[^\w]', '', word)
            if stripped and stripped[0].isupper():
                capitalized_terms.add(stripped.lower())

        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        if not memories:
            return []

        # -- Issue #3 fix: Always build index on FULL corpus for correct IDF --
        current_fp = self._compute_fingerprint(memories)
        if (self._indexed_version != self._corpus_version
                or self._doc_count != len(memories)
                or self._content_fingerprint != current_fp):
            self.build_index(memories)

        # -- Issue #2: Index-based candidate pre-filter ------------------------
        candidate_hashes = None
        if index_manager is not None:
            try:
                indexed = index_manager.search(query=original_query, limit=limit * 3)
                if indexed:
                    candidate_hashes = {h for h, _ in indexed}
            except Exception:
                pass  # Fall back to full scan

        # -- Inverted index candidate filtering --------------------------------
        # Use inverted index to collect candidates matching at least one query term
        inverted_candidates = set()
        if hasattr(self, '_inverted_index'):
            for token in query_tokens:
                inverted_candidates.update(self._inverted_index.get(token, set()))

        # -- Layers 1-4: Score each entry --------------------------------------
        # Pre-compute query categories ONCE (was called per-entry, 83% of search cost)
        _query_cats = set(_category_tagger.tag(query.lower()))

        results = []
        max_score = 0.0

        for mem in memories:
            # Issue #2: Skip non-candidates when index available
            if candidate_hashes is not None and mem.hash not in candidate_hashes:
                continue
            
            # Inverted index filtering: only score entries with at least one matching term
            # If no candidates found, fall back to scoring all entries (safety net)
            if inverted_candidates and mem.hash not in inverted_candidates:
                continue

            if category and mem.category != category:
                continue

            # Issue #3: Session/agent filtering moved to scoring loop (post-index)
            if session_id != "*":
                if getattr(mem, "session_id", "") != session_id:
                    continue
            if agent_id:
                mem_agent = getattr(mem, "agent_id", None)
                if mem_agent and mem_agent != agent_id:
                    continue
            
            score, matched, breakdown = self._score_entry(mem, query_tokens, query.lower(), capitalized_terms, _query_cats)
            
            if score <= 0:
                continue
            
            # Apply decay weighting if available
            if decay_fn:
                decay_score = decay_fn(mem)
                score *= (0.3 + 0.7 * decay_score)
                breakdown["decay_applied"] = round(decay_score, 3)
            
            max_score = max(max_score, score)
            results.append((mem, score, matched, breakdown))
        
        # -- Layer 5: Sliding Window scoring + Positional Salience ------------
        # Use cached windows built at index time (build_index()) instead of
        # rebuilding the entire window set on every search call. Falls back to
        # a live rebuild only when custom window params differ from the cached defaults.
        if use_windows and len(memories) >= 2:
            if (window_size == self._window_size and window_stride == self._window_stride
                    and self._cached_windows):
                windows = self._cached_windows
            else:
                _indexer = WindowIndexer(window_size=window_size, stride=window_stride)
                windows = _indexer.build_windows(memories)

            # Total number of windows for positional salience calculation
            total_windows = len(windows)

            entry_score_map: Dict[str, float] = {
                item[0].hash: item[1] for item in results
            }

            # Only score windows containing a TOP-K BM25 entry (not just score>0).
            # Previously, ANY entry with score>0 qualified its window, leading to 40-50%
            # of all windows being scored (e.g. 5,350/12,903). With top-K filtering,
            # only the ~50-100 best individual results qualify windows for scoring,
            # reducing window scoring from ~200ms to ~5-10ms on 12K+ stores.
            _WINDOW_TOP_K = min(limit * 3, 100)  # cap at 100 to bound cost
            if results:
                _sorted_results = sorted(results, key=lambda x: x[1], reverse=True)
                scored_hashes = {item[0].hash for item in _sorted_results[:_WINDOW_TOP_K]}
            else:
                scored_hashes = set()

            # Build hash->session_id lookup for session-aware window filtering
            _hash_to_session: Dict[str, str] = {}
            if session_id != "*":
                _hash_to_session = {
                    m.hash: getattr(m, "session_id", "")
                    for m in memories
                }

            window_raw: List[Tuple[object, float, List[str]]] = []
            for win in windows:
                # Skip windows with no top-K constituents
                if scored_hashes and not any(sid in scored_hashes for sid in win.source_ids):
                    continue
                if category and win.category != category:
                    continue
                # Session isolation: skip windows that contain entries from
                # other sessions. A window spanning multiple sessions would
                # leak cross-session content into filtered results.
                if session_id != "*" and _hash_to_session:
                    if any(
                        _hash_to_session.get(sid, "") != session_id
                        for sid in win.source_ids
                        if sid in _hash_to_session
                    ):
                        continue
                w_score, w_matched, _w_bd = self._score_entry(
                    win, query_tokens, query.lower(), capitalized_terms, _query_cats
                )
                if w_score <= 0:
                    continue
                if decay_fn:
                    try:
                        first_hash = win.source_ids[0]
                        first_entry = next(
                            m for m in memories if m.hash == first_hash
                        )
                        decay_score = decay_fn(first_entry)
                        w_score *= (0.3 + 0.7 * decay_score)
                    except StopIteration:
                        pass

                # -- Positional salience ----------------------------
                # First and last windows get a configurable boost.
                # These positions typically contain topic-setting and
                # concluding information that's most retrieval-relevant.
                if total_windows > 2 and positional_boost > 1.0:
                    if win.window_index == 0 or win.window_index == total_windows - 1:
                        w_score *= positional_boost

                max_score = max(max_score, w_score)
                window_raw.append((win, w_score, w_matched))

            # Deduplication: window vs constituent entries
            hashes_to_remove: set = set()
            for win, w_score, w_matched in window_raw:
                constituent_scores = [
                    entry_score_map.get(h, 0.0) for h in win.source_ids
                ]
                if constituent_scores and w_score > max(constituent_scores):
                    hashes_to_remove.update(win.source_ids)
                    results.append((win, w_score, w_matched, {"final_score": round(w_score, 4), "terms_matched": len(w_matched), "source": "window"}))

            if hashes_to_remove:
                results = [
                    item for item in results
                    if item[0].hash not in hashes_to_remove
                ]

        # -- Layer 7: Intent Reranker (post-scoring) --------------------------
        if use_layer7 and results:
            results = IntentReranker.rerank(original_query, results)

        # -- Layer 8: Qualifier & Negation Sensitivity (post-scoring) ---------
        if use_layer8 and results:
            results = QualifierAnalyzer.apply(original_query, results)

        # -- Normalize scores to 0-1 range ------------------------------------
        # Recalculate max_score after Layers 7+8 may have changed scores
        if results:
            max_score = max(score for _, score, *_ in results)

        if max_score > 0:
            normalized = []
            for item in results:
                mem, score, matched = item[0], item[1], item[2]
                breakdown = item[3] if len(item) > 3 else None
                relevance = score / max_score
                if relevance < min_score:
                    continue

                explanation = self._explain(matched, score, relevance, breakdown)
                result = SearchResult(
                    entry=mem,
                    score=score,
                    relevance=round(relevance, 4),
                    matched_terms=matched,
                    explanation=explanation,
                    _breakdown=breakdown or {},
                )
                normalized.append(result)
            results = normalized
        else:
            return []

        # -- Layer 9: Cross-Memory Clustering (post-normalization) ------------
        # Applied AFTER normalization so boost_neighbors() receives SearchResult
        # objects (which have .entry.hash, .score, .matched_terms, .explanation).
        if use_layer9 and results and self._cluster_index._built:
            results = self._cluster_index.apply_cluster_boost(results, top_k=10)

        # -- Layer 10: Pseudo-Relevance Feedback (PRF) ------------------------
        # Top-term extraction: take top-3 results, extract high-IDF terms that
        # appear in >=2 of those 3 docs, append up to 3 terms to re-score the
        # top-50 candidates. No second full BM25 pass - overhead ~3-8ms.
        if results and len(results) >= 3:
            results = self._prf_expand(results, query_tokens, limit)

        results.sort(key=lambda r: r.score, reverse=True)

        # -- Layer 11: Recency Boost for Competing Versions -------------------
        # When two results share high token overlap (Jaccard >= 0.40), they are
        # likely different phrasings of the same fact. In that case, boost the
        # newer entry and suppress the older one so updates surface above stale
        # facts. The 0.40 threshold catches semantic paraphrases while staying
        # tight enough to avoid false version detection between genuinely
        # distinct facts.
        if len(results) >= 2:
            def _jaccard_tokens(a: str, b: str) -> float:
                ta = set(re.findall(r'\w{2,}', a.lower()))
                tb = set(re.findall(r'\w{2,}', b.lower()))
                if not ta or not tb:
                    return 0.0
                return len(ta & tb) / len(ta | tb)

            RECENCY_OVERLAP_THRESHOLD = 0.40
            RECENCY_BOOST = 1.35
            RECENCY_SUPPRESS = 0.70

            # Collect timestamps for comparison
            def _ts(r) -> float:
                # SearchResult is a dataclass with .entry (MemoryEntry).
                # MemoryEntry stores timestamp as .created (ISO string).
                import datetime as _dt
                candidates = []
                if hasattr(r, 'entry') and r.entry is not None:
                    candidates.append(r.entry)
                candidates.append(r)
                for obj in candidates:
                    for attr in ('created', 'created_at', 'timestamp', 'date', 'ingested_at', 'ts'):
                        v = getattr(obj, attr, None)
                        if v is None:
                            continue
                        if isinstance(v, (int, float)):
                            return float(v)
                        try:
                            if isinstance(v, str) and v:
                                return _dt.datetime.fromisoformat(v.replace('Z', '+00:00')).timestamp()
                            if hasattr(v, 'timestamp'):
                                return v.timestamp()
                        except Exception:
                            pass
                return 0.0

            score_adjustments = {}  # index -> multiplier
            for i in range(min(len(results), 20)):  # only top-20 for perf
                for j in range(i + 1, min(len(results), 20)):
                    ri = results[i]
                    rj = results[j]
                    ci = ri.content  # SearchResult.content is a property -> entry.content
                    cj = rj.content
                    if _jaccard_tokens(ci, cj) >= RECENCY_OVERLAP_THRESHOLD:
                        ti = _ts(ri)
                        tj = _ts(rj)
                        if ti > tj:
                            # ri is newer -> boost ri, suppress rj
                            score_adjustments[i] = score_adjustments.get(i, 1.0) * RECENCY_BOOST
                            score_adjustments[j] = score_adjustments.get(j, 1.0) * RECENCY_SUPPRESS
                        elif tj > ti:
                            # rj is newer -> boost rj, suppress ri
                            score_adjustments[j] = score_adjustments.get(j, 1.0) * RECENCY_BOOST
                            score_adjustments[i] = score_adjustments.get(i, 1.0) * RECENCY_SUPPRESS

            if score_adjustments:
                import copy
                adjusted = []
                for idx, r in enumerate(results):
                    mult = score_adjustments.get(idx, 1.0)
                    if mult != 1.0:
                        cloned = copy.copy(r)
                        cloned.score = r.score * mult
                        # SearchResult uses matched_terms, not matched
                        mt = list(r.matched_terms) if hasattr(r, 'matched_terms') else []
                        mt.append('recency_boost' if mult > 1.0 else 'recency_suppress')
                        cloned.matched_terms = mt
                        adjusted.append(cloned)
                    else:
                        adjusted.append(r)
                results = sorted(adjusted, key=lambda r: r.score, reverse=True)

        # Result content dedup
        seen_content = set()
        deduped = []
        for r in results:
            # Normalize: lowercase, strip, first 200 chars
            content_key = r.entry.content.lower().strip()[:200]
            if content_key not in seen_content:
                seen_content.add(content_key)
                deduped.append(r)
        results = deduped
        
        return results[:limit]

    # -- Layer 10: Pseudo-Relevance Feedback ----------------------------------

    def _prf_expand(self, results: list, query_tokens: List[str],
                    limit: int) -> list:
        """Top-term PRF: expand query using terms from top-3 feedback docs.

        Algorithm:
          1. Take top-3 SearchResult objects as feedback documents.
          2. Count how many feedback docs each candidate term appears in.
          3. Keep terms with doc_freq >= 2 (must appear in >=2 of 3 docs)
             AND idf >= 1.5 (filter out common corpus terms).
          4. Take the top-3 expansion terms by combined score.
          5. Re-score only the top-50 candidates with the expanded token set,
             weighting PRF terms at 1x (vs search_queries at 3x).
          6. Blend: 70% original BM25 score, 30% PRF re-score.

        No second full BM25 pass - only top-50 candidates are re-scored.
        Overhead: +3-8ms.
        """
        # Only apply PRF to direct MemoryEntry results (not WindowEntry from Layer 5).
        # WindowEntry objects don't have a hash in the embeddings/token cache.
        from .entry import MemoryEntry as _MemoryEntry
        if results and not isinstance(results[0].entry, _MemoryEntry):
            return results

        # Collect token sets from top-3 feedback docs
        feedback = results[:3]
        query_token_set = set(query_tokens)

        # Count doc frequency for each term in feedback docs
        term_doc_freq: Counter = Counter()
        for r in feedback:
            cached = self._token_cache.get(r.entry.hash)
            if cached:
                doc_terms = set(cached[0])  # content tokens (unweighted)
            else:
                doc_terms = set(self._tokenize(r.entry.content))
            for t in doc_terms:
                if t not in query_token_set and t not in self.STOPWORDS:
                    term_doc_freq[t] += 1

        # Select expansion terms: appear in >=2 feedback docs AND high IDF
        candidates = [
            (t, cnt) for t, cnt in term_doc_freq.items()
            if cnt >= 2 and self._idf_cache.get(t, 0) >= 1.5
        ]
        if not candidates:
            return results

        # Rank by IDF (rarity) - rare terms are more discriminative
        expansion_terms = sorted(
            candidates,
            key=lambda x: self._idf_cache.get(x[0], 0),
            reverse=True,
        )[:3]
        if not expansion_terms:
            return results

        prf_tokens = query_tokens + [t for t, _ in expansion_terms]

        # Re-score top-50 candidates only (not full corpus)
        top50 = results[:50]
        prf_rescored = []
        for r in top50:
            prf_score, _, _prf_bd = self._score_entry(
                r.entry, prf_tokens, " ".join(prf_tokens),
            )
            # 70/30 blend: preserve original BM25 ranking, PRF nudges
            blended = 0.7 * r.score + 0.3 * prf_score
            prf_rescored.append(SearchResult(
                entry=r.entry,
                score=blended,
                relevance=r.relevance,
                matched_terms=r.matched_terms + [f"prf:{t}" for t, _ in expansion_terms],
                explanation=r.explanation,
            ))

        prf_rescored.sort(key=lambda r: r.score, reverse=True)
        # Merge re-scored top-50 with remainder (positions 51+)
        return prf_rescored + results[50:]

    def _score_entry(self, mem, query_tokens: List[str], query_lower: str,
                     capitalized_terms: Optional[set] = None,
                     query_categories: Optional[set] = None) -> Tuple[float, List[str], dict]:
        """Score a single memory against query tokens.

        Layer 4 additions:
        - Rarity boost: multiply each term's BM25 contribution by
          ``_rarity_boost(term)`` so that rare/specific terms carry more weight.
        - Proper-noun boost: if a term was written in Title-case in the
          original query (detected via *capitalized_terms*) it receives an
          additional 1.5x multiplier, rewarding matches on named entities.
        """
        if capitalized_terms is None:
            capitalized_terms = set()

        # M-6 fix (part 2): For WindowEntry, resolve to the constituent with the
        # best raw content match instead of scoring the aggregate window directly.
        # WindowEntry hashes are never in _token_cache, so this avoids the
        # uncached fallback path which can under-score them.
        try:
            from .windowing import WindowEntry as _WindowEntry
            if isinstance(mem, _WindowEntry):
                _entries = getattr(mem, 'entries', None)
                if _entries:
                    best = max(
                        _entries,
                        key=lambda e: len(set(query_tokens) & set(self._tokenize(e.content))),
                        default=None,
                    )
                    if best:
                        mem = best
        except ImportError:
            pass

        content_lower = mem.content.lower()

        # use cached tokenization from build_index() when available
        _cached = self._token_cache.get(mem.hash) if hasattr(self, '_token_cache') else None
        if _cached is not None:
            content_tokens, enriched_tokens, all_tokens, tf_counter, enriched_lower = _cached
        else:
            content_tokens = self._tokenize(mem.content)
            enriched_tokens = []
            enriched_lower = ""
            if getattr(mem, "enriched_summary", ""):
                enriched_tokens = self._tokenize(mem.enriched_summary)
                enriched_lower = mem.enriched_summary.lower()
            keyword_tokens = []
            if getattr(mem, "search_keywords", []):
                for kw in mem.search_keywords:
                    keyword_tokens.extend(self._tokenize(str(kw)))
            # M-6 fix (part 1): include search_queries tokens in uncached path
            # (tripled for 3x boost via repetition, matching build_index weighting exactly)
            sq_tokens = []
            for sq in (getattr(mem, "search_queries", []) or []):
                sq_tokens.extend(self._tokenize(str(sq)))
            all_tokens = content_tokens + enriched_tokens + keyword_tokens + keyword_tokens + sq_tokens + sq_tokens + sq_tokens  # keywords 2x
            tf_counter = Counter(all_tokens)

        doc_len = len(all_tokens)
        
        score = 0.0
        matched = []
        
        # Enriched summary field boost: track how many query terms
        # appear in the enriched_summary specifically (not just raw content).
        # Enriched summaries are LLM-generated clean descriptions - matches there
        # are higher-quality signals than matches in noisy raw conversation text.
        _enriched_term_hits = 0
        if enriched_lower:
            _enriched_token_set = set(enriched_tokens) if enriched_tokens else set()
            for qt in query_tokens:
                if qt in _enriched_token_set:
                    _enriched_term_hits += 1

        # BM25 scoring per query term
        for term in query_tokens:
            tf = tf_counter.get(term, 0)
            if tf == 0:
                continue

            matched.append(term)
            idf = self._idf_cache.get(term, 1.0)

            # N-gram IDF boost: bigrams get 1.5x, trigrams get 2.0x
            underscore_count = term.count('_')
            if underscore_count == 1:      # bigram
                idf *= 1.5
            elif underscore_count >= 2:    # trigram (or higher)
                idf *= 2.0

            # BM25+ TF component with length normalization.
            # delta=1.0 floor prevents near-zero scores on legitimate partial matches.
            tf_norm = _BM25_DELTA + (tf * (self.k1 + 1)) / (
                tf + self.k1 * (1 - self.b + self.b * doc_len / max(self._avg_doc_len, 1))
            )

            # Layer 4 - rarity boost (amplifies rare/specific terms)
            rarity = self._rarity_boost(term)
            # Layer 4 - proper-noun boost (enhanced 3.0x for mid-sentence capitalised terms)
            proper = 3.0 if self._is_proper_noun(term, capitalized_terms) else 1.0
            
            # Layer 4.1 - specific identifier boost (quoted strings, specific patterns)
            specific_boost = self._specific_identifier_boost(term, mem.content)

            score += idf * tf_norm * rarity * proper * specific_boost
        
        # Exact phrase bonus (query tokens appear consecutively in content tokens)
        if len(query_tokens) > 1:
            content_token_str = " ".join(content_tokens)
            query_token_str = " ".join(query_tokens)
            if query_token_str in content_token_str:
                score *= 1.5
            elif enriched_tokens and query_token_str in " ".join(enriched_tokens):
                score *= 1.3  # Slightly lower boost for enriched phrase match
        
        # Field boosting: check tags (at most one 1.2x boost per entry)
        if hasattr(mem, 'tags') and mem.tags:
            tag_text = " ".join(mem.tags).lower()
            tag_hit = False
            for term in query_tokens:
                if term in tag_text:
                    if not tag_hit:
                        score *= 1.2
                        tag_hit = True
                    if term not in matched:
                        matched.append(f"tag:{term}")

        # Category boost - 1.3x when query belongs to same semantic category
        # query_categories pre-computed once per search, passed in (was 83% of latency)
        if score > 0 and query_categories and hasattr(mem, 'tags') and mem.tags:
            entry_categories = set(mem.tags) & set(_category_tagger.rules.keys())
            if query_categories & entry_categories:
                score *= 1.3
                matched.append("category_boost")

        # Field boosting: check source (at most one 1.1x boost per entry)
        if hasattr(mem, 'source') and mem.source:
            source_lower = mem.source.lower()
            for term in query_tokens:
                if term in source_lower:
                    score *= 1.1
                    break
        
        # Source-weighted scoring: structured memories rank higher
        _source = getattr(mem, 'source', '') or ''
        _mtype = getattr(mem, 'memory_type', 'episodic') or 'episodic'
        if _mtype in ('mistake', 'fact', 'procedure', 'preference'):
            score *= 1.15  # structured knowledge boost
        elif _source.startswith('pipeline:'):
            score *= 0.85  # raw conversation penalty
        
        # Enrichment quality boosts REMOVED (non-discriminative).
        # "Has enriched_summary" (1.10x) applied to 96% of entries - uniform
        # scaling adds no ranking value. "Rich keywords" (1.05x) same problem.
        # These were flattening score distribution without improving recall.
        # See the retrieval-quality diagnosis, Issue 5.
        # if getattr(mem, 'enriched_summary', ''):
        #     score *= 1.10
        # if len(getattr(mem, 'search_keywords', []) or []) > 5:
        #     score *= 1.05

        # initialize boost trackers before conditional blocks
        _enrich_boost = 1.0
        _sq_boost = 1.0
        anti_adj_penalty = 1.0

        # Enriched summary field boost: reduced from 1.45x to 1.25x.
        # Enriched terms already get 2x TF weight in all_tokens; 1.45x on top
        # created 2.9x effective weight causing double-counting. 1.25x cap
        # preserves the signal without over-weighting vs search_queries.
        if _enriched_term_hits > 0 and score > 0:
            _enrich_boost = min(1.0 + _enriched_term_hits * 0.10, 1.25)  # was 0.15, 1.45
            score *= _enrich_boost
            if "enriched_match" not in matched:
                matched.append("enriched_match")

        # Search queries field boost: reduced from 1.60x to 1.40x.
        # SQ tokens already get 3x TF weight in all_tokens. Reducing the
        # multiplicative boost prevents over-weighting vs content relevance.
        _search_queries = getattr(mem, 'search_queries', []) or []
        if _search_queries and score > 0:
            _sq_text = " ".join(_search_queries).lower()
            _sq_tokens = set(self._tokenize(_sq_text))
            _sq_hits = sum(1 for qt in query_tokens if qt in _sq_tokens)
            if _sq_hits > 0:
                _sq_boost = min(1.0 + _sq_hits * 0.15, 1.40)  # was 0.20, 1.60
                score *= _sq_boost
                if "doc2query_match" not in matched:
                    matched.append("doc2query_match")

        # Layer 11 - Anti-adjacency penalty
        # Penalizes results that match the TOPIC but not the specific ANSWER
        if score > 0:
            anti_adj_penalty = self._anti_adjacency_penalty(query_lower, content_lower)
            if anti_adj_penalty < 1.0:
                score *= anti_adj_penalty
                if "anti_adjacency_penalty" not in matched:
                    matched.append("anti_adjacency_penalty")

        # Score breakdown for transparency
        # Track per-component contributions as numeric values.
        # Use locally-scoped tracking variables instead of dir() introspection.
        _applied_enrich_boost = _enrich_boost if _enriched_term_hits > 0 else 1.0
        _applied_sq_boost = _sq_boost if ("doc2query_match" in matched) else 1.0
        _applied_anti_adj = anti_adj_penalty if ("anti_adjacency_penalty" in matched) else 1.0
        _applied_phrase = 1.5 if (len(query_tokens) > 1 and " ".join(query_tokens) in " ".join(content_tokens)) else (1.3 if (len(query_tokens) > 1 and enriched_tokens and " ".join(query_tokens) in " ".join(enriched_tokens)) else 1.0)

        breakdown = {
            "final_score": round(score, 4),
            "terms_matched": len([m for m in matched if not m.startswith("tag:") and m not in ("category_boost", "enriched_match", "doc2query_match", "anti_adjacency_penalty")]),
            "phrase_boost": _applied_phrase,
            "tag_boost": 1.2 if "tag:" in " ".join(matched) else 1.0,
            "category_boost": 1.3 if "category_boost" in matched else 1.0,
            "enriched_boost": round(_applied_enrich_boost, 3),
            "doc2query_boost": round(_applied_sq_boost, 3),
            "structured_boost": 1.15 if _mtype in ('mistake', 'fact', 'procedure', 'preference') else (0.85 if _source.startswith('pipeline:') else 1.0),
            "anti_adjacency": round(_applied_anti_adj, 3),
        }

        return score, matched, breakdown

    @staticmethod
    def _ngrams(tokens: List[str], n: int) -> List[str]:
        """Generate n-grams from a token list, joined by underscore."""
        if len(tokens) < n:
            return []
        return ["_".join(tokens[i:i + n]) for i in range(len(tokens) - n + 1)]

    def _tokenize(self, text: str) -> List[str]:
        """Tokenize text into lowercase terms, filtering stopwords.

        Returns unigrams + bigrams + trigrams.
        """
        raw = re.findall(r'\w{2,}', text.lower())
        unigrams = [
            _norm(t) for t in raw
            if t not in self.STOPWORDS and not (t.isdigit() and len(t) < 3)
        ]

        bigrams = [
            bg for bg in self._ngrams(unigrams, 2)
            if not all(p in self.STOPWORDS for p in bg.split('_'))
        ]
        trigrams = [
            tg for tg in self._ngrams(unigrams, 3)
            if not all(p in self.STOPWORDS for p in tg.split('_'))
        ]

        return unigrams + bigrams + trigrams
    
    def _explain(self, matched: List[str], score: float, relevance: float,
                 breakdown: dict = None) -> str:
        """Generate a human-readable explanation of the score.
        
        Args:
            matched: List of matched query terms.
            score: Raw BM25+ score before normalization.
            relevance: Normalized 0-1 relevance score.
            breakdown: Optional per-component score breakdown dict.
        """
        parts = [f"matched: {', '.join(matched)}"]
        parts.append(f"raw={score:.3f}")
        parts.append(f"relevance={relevance:.2f}")
        if breakdown:
            components = []
            for key, val in breakdown.items():
                if isinstance(val, float):
                    components.append(f"{key}={val:.3f}")
                else:
                    components.append(f"{key}={val}")
            parts.append("breakdown: " + ", ".join(components))
        return " | ".join(parts)
    
    # -- Index Persistence (Incremental Index Loading) ---------------

    def save_index(self, path: str) -> None:
        """Persist the BM25 index statistics to *path* as JSON.

        Serialises IDF cache, doc-frequency counts, corpus size, and average
        document length so that a subsequent ``load_index()`` can restore the
        search engine state without reprocessing all memories.

        Uses an atomic write (write-to-temp then rename) to prevent
        partial/corrupt index files on crash.

        Args:
            path: Destination file path (e.g. ``{workspace}/.parsica_index.json``).
        """
        import tempfile
        data = {
            "version": "4.6.0",
            "idf_cache": self._idf_cache,
            "avg_doc_len": self._avg_doc_len,
            "doc_count": self._doc_count,
            "doc_freqs": dict(self._doc_freqs),
            "corpus_version": self._corpus_version,
            "indexed_version": self._indexed_version,
            "content_fingerprint": self._content_fingerprint,
        }
        dir_name = os.path.dirname(os.path.abspath(path))
        os.makedirs(dir_name, exist_ok=True)
        try:
            with tempfile.NamedTemporaryFile(
                "w", dir=dir_name, delete=False, suffix=".tmp", encoding="utf-8"
            ) as tf:
                import json as _json
                _json.dump(data, tf, ensure_ascii=False)
                tmp_path = tf.name
            os.replace(tmp_path, path)
        except OSError:
            pass  # non-fatal: next startup will fall back to full build

    def load_index(self, path: str) -> bool:
        """Load a previously saved BM25 index from *path*.

        Restores IDF cache, doc-frequency counts, corpus size, and average
        document length.

        Args:
            path: Source file path (e.g. ``{workspace}/.parsica_index.json``).

        Returns:
            ``True`` if the index was loaded successfully, ``False`` otherwise
            (file not found, JSON error, schema mismatch, etc.).
        """
        import json as _json
        if not os.path.exists(path):
            return False
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = _json.load(fh)
            # Validate schema presence
            for required in ("idf_cache", "avg_doc_len", "doc_count", "doc_freqs"):
                if required not in data:
                    return False
            self._idf_cache = data["idf_cache"]
            self._avg_doc_len = float(data["avg_doc_len"])
            self._doc_count = int(data["doc_count"])
            self._doc_freqs = Counter(data["doc_freqs"])
            self._corpus_version = int(data.get("corpus_version", 0))
            self._indexed_version = int(data.get("indexed_version", 0))
            self._content_fingerprint = int(data.get("content_fingerprint", 0))
            return True
        except Exception:
            return False

    def apply_wal_entries(self, entries: list) -> None:
        """Incrementally update the BM25 index with *entries*.

        Called on startup to fold WAL entries (ingested since the last index
        save) into the in-memory index without rebuilding from scratch.

        The update is an approximation: IDF values are recomputed after all
        new documents are processed so the result is numerically identical to
        calling ``build_index()`` on the full corpus.

        Args:
            entries: List of :class:`~parsica_spirit.engine.entry.MemoryEntry` objects
                     to incorporate.  Entries already reflected in the saved
                     index should **not** be included.
        """
        if not entries:
            return

        # Add each new document's term contributions.
        # Replicate the exact multi-field weighting from build_index() so that
        # WAL-replayed entries produce the same IDF as a full index rebuild.
        for entry in entries:
            tokens = self._tokenize(entry.content)

            # enriched_summary at 2x weight (same as build_index)
            enriched_tokens = self._tokenize(entry.enriched_summary) if getattr(entry, "enriched_summary", "") else []

            # search_keywords at 1x weight
            keyword_tokens = []
            if getattr(entry, "search_keywords", []):
                for kw in entry.search_keywords:
                    keyword_tokens.extend(self._tokenize(str(kw)))

            # search_queries at 3x weight (same as build_index)
            sq_tokens = []
            for sq in (getattr(entry, "search_queries", []) or []):
                sq_tokens.extend(self._tokenize(str(sq)))

            # Field-weighted all_tokens matching build_index() exactly
            all_tokens = (
                tokens
                + enriched_tokens + enriched_tokens          # 2x enriched_summary
                + keyword_tokens + keyword_tokens            # 2x search_keywords
                + sq_tokens + sq_tokens + sq_tokens          # 3x search_queries
            )

            self._doc_count += 1
            # Running average of document length (uses weighted len, matching build_index)
            self._avg_doc_len = (
                (self._avg_doc_len * (self._doc_count - 1) + len(all_tokens))
                / self._doc_count
            )
            # doc_freqs uses set(all_tokens) so repetition doesn't skew IDF
            for term in set(all_tokens):
                self._doc_freqs[term] = self._doc_freqs.get(term, 0) + 1

        # Recompute IDF for all terms (BM25 formula)
        for term, df in self._doc_freqs.items():
            self._idf_cache[term] = math.log(
                (self._doc_count - df + 0.5) / (df + 0.5) + 1.0
            )

        self._indexed_version = self._corpus_version

    def mark_dirty(self) -> None:
        """Mark the corpus as changed. Next search() will rebuild the index."""
        self._corpus_version += 1
    
    def reindex(self, memories: list) -> None:
        """Force a full index rebuild."""
        self._corpus_version += 1
        self.build_index(memories)
    
    def stats(self) -> Dict:
        """Return index statistics.

        Includes cluster index stats.
        """
        base = {
            "doc_count": self._doc_count,
            "avg_doc_len": round(self._avg_doc_len, 1),
            "vocab_size": len(self._doc_freqs),
            "top_terms": self._doc_freqs.most_common(20),
        }
        base["clusters"] = self._cluster_index.stats()
        return base


# ===========================================================================
# Cross-channel recency search
# ===========================================================================

def search_by_recency(
    entries: List,
    hours: float = 6.0,
    limit: int = 5,
    exclude_channel: Optional[str] = None
) -> List:
    """
    Return the most recent MemoryEntry objects within the given time window.
    Sorted by created timestamp descending.
    Optionally exclude entries from a specific source_channel.

    Handles both naive timestamps (treated as local time, which is what
    MemoryEntry uses by default via ``datetime.now().isoformat()``) and
    aware timestamps (compared in UTC).
    """
    from datetime import datetime, timezone, timedelta
    now_utc = datetime.now(timezone.utc)
    now_naive = datetime.now()
    cutoff_utc = now_utc - timedelta(hours=hours)
    cutoff_naive = now_naive - timedelta(hours=hours)
    results = []
    for entry in entries:
        if not entry.created:
            continue
        try:
            ts = entry.created
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                # Naive timestamp - compare against naive local cutoff
                if dt < cutoff_naive:
                    continue
            else:
                # Aware timestamp - compare against UTC cutoff
                if dt < cutoff_utc:
                    continue
        except (ValueError, AttributeError):
            continue
        if exclude_channel is not None and exclude_channel and getattr(entry, "source_channel", "") == exclude_channel:
            continue
        results.append(entry)
    results.sort(key=lambda e: e.created, reverse=True)
    return results[:limit]
