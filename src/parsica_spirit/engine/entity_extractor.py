"""
Parsica Spirit engine - Entity Extractor
==================================

Zero-dependency named entity extraction using heuristics and patterns.
Designed for agent conversation text: project names, people, versions,
URLs, and domain-specific concepts.

Extracts four entity types:
  PERSON   - capitalized name patterns
  PROJECT  - versioned names, code identifiers, product names
  CONCEPT  - technical terms, recurring capitalized nouns
  DATE     - dates, timestamps, relative time references

All methods are non-fatal. Bad input returns empty results, never raises.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple


# -- Entity types ------------------------------------------------------------

PERSON   = "PERSON"
PROJECT  = "PROJECT"
CONCEPT  = "CONCEPT"
DATE     = "DATE"
VERSION  = "VERSION"
URL      = "URL"


@dataclass
class Entity:
    text: str
    entity_type: str
    canonical: str          # normalised form (lowercase for lookup)
    confidence: float       # 0.0-1.0
    positions: List[int] = field(default_factory=list)  # char offsets


# -- Stopwords (won't be extracted as entities) -------------------------------

_STOPWORDS: Set[str] = frozenset({
    'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
    'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
    'should', 'may', 'might', 'shall', 'can', 'to', 'of', 'in', 'for',
    'on', 'with', 'at', 'by', 'from', 'as', 'into', 'through', 'during',
    'before', 'after', 'above', 'below', 'between', 'out', 'off', 'over',
    'under', 'again', 'further', 'then', 'once', 'here', 'there', 'when',
    'where', 'why', 'how', 'all', 'each', 'every', 'both', 'few', 'more',
    'most', 'other', 'some', 'such', 'no', 'nor', 'not', 'only', 'own',
    'same', 'so', 'than', 'too', 'very', 'just', 'because', 'but', 'and',
    'or', 'if', 'while', 'about', 'up', 'it', 'its', 'he', 'she', 'they',
    'them', 'his', 'her', 'my', 'your', 'our', 'their', 'this', 'that',
    'these', 'those', 'i', 'me', 'we', 'you', 'what', 'which', 'who',
    'whom', 'whose', 'yes', 'no', 'ok', 'okay', 'thanks', 'thank', 'please',
    'also', 'however', 'therefore', 'thus', 'hence', 'otherwise', 'since',
    'note', 'see', 'use', 'using', 'used', 'like', 'new', 'good', 'great',
    'true', 'false', 'none', 'null', 'zero', 'one', 'two', 'three', 'done',
    'let', 'go', 'get', 'set', 'run', 'add', 'fix', 'bug', 'now', 'then',
})

# Words that look capitalised at sentence start but aren't entities
_SENTENCE_STARTERS: Set[str] = frozenset({
    'this', 'that', 'these', 'those', 'when', 'while', 'after', 'before',
    'during', 'although', 'because', 'since', 'unless', 'until', 'however',
    'therefore', 'furthermore', 'moreover', 'nevertheless', 'nonetheless',
    'finally', 'firstly', 'secondly', 'thirdly', 'lastly', 'overall',
    'additionally', 'consequently', 'specifically', 'generally', 'typically',
    'currently', 'previously', 'recently', 'today', 'tomorrow', 'yesterday',
})


# -- Compiled regex patterns --------------------------------------------------

# Semantic version: v4.7.0, 4.7.0, v4, 2.0.1
_RE_VERSION = re.compile(
    r'\bv?(\d+)\.(\d+)(?:\.(\d+))?(?:[-.](?:alpha|beta|rc|dev|post)\d*)?\b',
    re.IGNORECASE,
)

# URL pattern
_RE_URL = re.compile(
    r'https?://[^\s<>"\'()]+|www\.[^\s<>"\'()]+',
    re.IGNORECASE,
)

# Date patterns: 2026-03-02, March 2, 2026, Mar 2nd, Feb 14
_RE_DATE = re.compile(
    r'\b(?:\d{4}-\d{2}-\d{2}|'
    r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?|'
    r'\d{1,2}(?:st|nd|rd|th)?\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*(?:,?\s+\d{4})?)\b',
    re.IGNORECASE,
)

# CamelCase identifiers (likely code/project names): MemorySystem, ShardManager
_RE_CAMEL = re.compile(r'\b[A-Z][a-z]+(?:[A-Z][a-z]+)+\b')

# ALL_CAPS identifiers: BM25, RBAC, PPMI, API, TF-IDF (min 2 chars, max 8)
# Allow digits in acronyms: BM25, TF-IDF, API3, HTTP2
_RE_ALLCAPS = re.compile(r'\b[A-Z][A-Z0-9]{1,7}(?:-[A-Z][A-Z0-9]{1,7})?\b')

# snake_case or kebab-case identifiers: auth_v2, parsica-spirit
_RE_CODE_ID = re.compile(r'\b[a-z][a-z0-9]*(?:[_-][a-z0-9]+){1,4}\b')

# Consecutive capitalised words (proper noun phrases): Acme Analytics, Mac Mini
_RE_PROPER_NOUN = re.compile(
    r'\b(?:[A-Z][a-z]{1,20}\s+){1,3}[A-Z][a-z]{1,20}\b'
)

# Single capitalised word (potential entity when not at sentence start)
_RE_CAPITAL = re.compile(r'\b[A-Z][a-z]{2,20}\b')

# File path patterns: /path/to/file.py, ./relative/path
_RE_FILEPATH = re.compile(r'(?:^|(?<=\s))(?:[.~/]|[A-Za-z]:[\\/])[^\s"\'<>|]+')


# -- Alias / canonical mapping ------------------------------------------------

# Known aliases: normalise variant mentions to a canonical form.
# Ships as GENERIC EXAMPLES demonstrating each alias shape (spaced/joined, kebab/snake,
# dotted legacy id -> canonical) - replace with your own org/project vocabulary.
_KNOWN_ALIASES: Dict[str, str] = {
    "acme analytics": "acme analytics",
    "acme": "acme analytics",
    "parsica-spirit": "parsica-spirit",
    "acmebot.suite.memory": "parsica-spirit",
    "acme-guard": "acme-guard",
    "acme_guard": "acme-guard",
    "acme-suite": "acme-suite",
    "acme_suite": "acme-suite",
    "acme-pipeline": "acme-pipeline",
    "acme_pipeline": "acme-pipeline",
    "memorysystem": "memorysystem",
    "memory system": "memorysystem",
    "acmebot": "acmebot",
    "acme bot": "acmebot",
    "mac mini": "mac mini",
    "mac studio": "mac studio",
}


def _canonical(text: str) -> str:
    """Normalise entity text to canonical form."""
    lower = text.lower().strip()
    return _KNOWN_ALIASES.get(lower, lower)


def _is_stopword(text: str) -> bool:
    return text.lower() in _STOPWORDS or text.lower() in _SENTENCE_STARTERS


# -- EntityExtractor ----------------------------------------------------------

class EntityExtractor:
    """
    Heuristic named entity extractor for agent conversation text.

    Design principles:
    - Zero external dependencies (stdlib only)
    - Prefers precision over recall (fewer false positives)
    - Non-fatal: returns [] on any error
    - Deterministic: same input -> same output every time
    """

    def extract(self, text: str) -> List[Entity]:
        """Extract all entities from text. Returns deduplicated list."""
        try:
            if not text or not text.strip():
                return []
            entities: List[Entity] = []
            seen: Set[str] = set()  # canonical forms already added

            def _add(ent: Entity) -> None:
                if ent.canonical not in seen and len(ent.text.strip()) >= 2:
                    seen.add(ent.canonical)
                    entities.append(ent)

            # 1. URLs (highest priority - exact, high confidence)
            for m in _RE_URL.finditer(text):
                url = m.group()
                can = _canonical(url)
                _add(Entity(url, URL, can, 0.95, [m.start()]))

            # 2. Dates
            for m in _RE_DATE.finditer(text):
                dt = m.group()
                _add(Entity(dt, DATE, dt.lower(), 0.85, [m.start()]))

            # 3. Semantic versions
            for m in _RE_VERSION.finditer(text):
                ver = m.group()
                _add(Entity(ver, VERSION, ver.lower(), 0.9, [m.start()]))

            # 4. CamelCase identifiers (code/project names)
            for m in _RE_CAMEL.finditer(text):
                word = m.group()
                if not _is_stopword(word):
                    can = _canonical(word)
                    _add(Entity(word, PROJECT, can, 0.75, [m.start()]))

            # 5. ALL_CAPS acronyms (technical terms: BM25, RBAC, API)
            for m in _RE_ALLCAPS.finditer(text):
                word = m.group()
                # Skip common non-entities
                if word in {'I', 'A', 'AT', 'OR', 'AND', 'NOT', 'OK', 'NO',
                            'YES', 'PM', 'AM', 'PST', 'UTC', 'GMT', 'ID',
                            'US', 'UK', 'EU', 'DO', 'GO', 'TO', 'IN', 'OF'}:
                    continue
                can = _canonical(word)
                _add(Entity(word, CONCEPT, can, 0.7, [m.start()]))

            # 6. Proper noun phrases (consecutive capitalised words)
            for m in _RE_PROPER_NOUN.finditer(text):
                phrase = m.group().strip()
                if not _is_stopword(phrase.split()[0]):
                    can = _canonical(phrase)
                    _add(Entity(phrase, PERSON, can, 0.65, [m.start()]))

            # 7. snake_case / kebab-case code identifiers
            for m in _RE_CODE_ID.finditer(text):
                word = m.group()
                if len(word) >= 4 and not _is_stopword(word):
                    can = _canonical(word)
                    _add(Entity(word, PROJECT, can, 0.6, [m.start()]))

            # 8. Single capitalised words not at sentence start
            for m in _RE_CAPITAL.finditer(text):
                word = m.group()
                # Only add if not at start of sentence and not already seen
                pos = m.start()
                preceding = text[:pos].rstrip()
                at_sentence_start = (pos == 0 or
                                     preceding.endswith('.') or
                                     preceding.endswith('!') or
                                     preceding.endswith('?') or
                                     preceding.endswith('\n'))
                if not at_sentence_start and not _is_stopword(word):
                    can = _canonical(word)
                    if can not in seen:
                        _add(Entity(word, CONCEPT, can, 0.45, [pos]))

            return entities

        except Exception:
            return []

    def extract_relations(
        self, text: str, entities: List[Entity]
    ) -> List[Tuple[str, str, str]]:
        """
        Extract relationships between entities as (subject, relation, object) triples.

        Relations detected:
          - "X and Y" / "X with Y" -> co_mentioned
          - "X's Y" / "X of Y" -> associated_with
          - "X built/created/wrote/developed Y" -> created_by
          - "X uses/depends on Y" -> depends_on
          - "X in/at Y" / "X on Y" -> located_in / part_of
        """
        try:
            if len(entities) < 2:
                return []

            relations: List[Tuple[str, str, str]] = []
            seen_pairs: Set[Tuple[str, str]] = set()

            # Build a sorted entity list by position for proximity checks.
            # Entities with no recorded positions get position=-1 (unknown),
            # which excludes them from proximity-based relation extraction.
            # Previously, the fallback was position=0, which falsely placed
            # unpositioned entities at sentence-initial offset and caused
            # incorrect co_mentioned relations with entities near offset 0.
            positioned = sorted(
                [(e.positions[0] if e.positions else -1, e) for e in entities],
                key=lambda x: x[0]
            )

            # Proximity co-mention: entities within 80 chars are "co_mentioned".
            # Skip entities with position=-1 (unknown) - they cannot participate
            # in proximity-based relation extraction.
            for i, (pos_a, ent_a) in enumerate(positioned):
                if pos_a < 0:
                    continue  # unknown position - skip
                for pos_b, ent_b in positioned[i + 1:]:
                    if pos_b < 0:
                        continue  # unknown position - skip
                    if abs(pos_b - pos_a) > 80:
                        break
                    pair = (min(ent_a.canonical, ent_b.canonical),
                            max(ent_a.canonical, ent_b.canonical))
                    if pair not in seen_pairs:
                        seen_pairs.add(pair)
                        relations.append((ent_a.canonical, "co_mentioned",
                                          ent_b.canonical))

            # Verb-based relations using regex
            _RE_CREATED = re.compile(
                r'(\w[\w\s-]{0,30}?)\s+(?:built|created|wrote|developed|shipped|made|authored)\s+(\w[\w\s-]{0,30})',
                re.IGNORECASE,
            )
            _RE_USES = re.compile(
                r'(\w[\w\s-]{0,30}?)\s+(?:uses|using|depends on|requires|integrates)\s+(\w[\w\s-]{0,30})',
                re.IGNORECASE,
            )

            entity_texts_lower = {e.text.lower(): e.canonical for e in entities}

            def _find_entity(span: str) -> Optional[str]:
                """Try to match span text to a known entity canonical."""
                span_l = span.lower().strip()
                for txt, can in entity_texts_lower.items():
                    if txt in span_l or span_l in txt:
                        return can
                return None

            for m in _RE_CREATED.finditer(text):
                subj = _find_entity(m.group(1))
                obj  = _find_entity(m.group(2))
                if subj and obj and subj != obj:
                    relations.append((subj, "created_by", obj))

            for m in _RE_USES.finditer(text):
                subj = _find_entity(m.group(1))
                obj  = _find_entity(m.group(2))
                if subj and obj and subj != obj:
                    relations.append((subj, "depends_on", obj))

            return relations

        except Exception:
            return []

    def merge_aliases(self, entities: List[Entity]) -> List[Entity]:
        """
        Merge entities that refer to the same thing (same canonical form).
        Returns a deduplicated list, keeping the highest-confidence mention.
        """
        try:
            by_canonical: Dict[str, Entity] = {}
            for ent in entities:
                existing = by_canonical.get(ent.canonical)
                if existing is None or ent.confidence > existing.confidence:
                    by_canonical[ent.canonical] = Entity(
                        text=ent.text,
                        entity_type=ent.entity_type,
                        canonical=ent.canonical,
                        confidence=ent.confidence,
                        positions=existing.positions + ent.positions if existing else ent.positions,
                    )
            return list(by_canonical.values())
        except Exception:
            return entities
