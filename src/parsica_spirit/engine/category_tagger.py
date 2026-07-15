"""
Category Auto-Tagger for the Parsica Spirit engine.

Zero-dependency, keyword-rule-based semantic category tagging.
Classifies text into 20+ domain categories to enable category-boosted
search recall - bridging the lexical gap between queries like
"how much are we spending?" and entries like "AWS bill $4,300".

Usage:
    tagger = CategoryTagger()
    tags = tagger.tag("gym membership renews $49.99 monthly")
    # -> ['financial', 'fitness']
    primary = tagger.primary_category("AWS bill $4,300")
    # -> 'financial'
"""

import re
from typing import Dict, List, Tuple


# ---------------------------------------------------------------------------
# Category keyword rules
# Each entry: category_name -> list of keyword/patterns
# Matching is case-insensitive substring search unless prefixed with "re:"
# ---------------------------------------------------------------------------

_CATEGORY_RULES: Dict[str, List[str]] = {
    "financial": [
        r"\$",           # dollar sign (regex)
        "cost", "price", "fee", "budget", "expense", "bill",
        "payment", "subscription", "salary", "invoice", "spend",
        "spending", "revenue", "profit", "loss", "tax", "refund",
        "charge", "rate", "quote", "estimate", "deposit", "withdraw",
        "transfer", "paycheck", "payroll", "reimburse", "afford",
        "cheap", "expensive", "discount", "coupon", "deal",
    ],
    "medical": [
        "doctor", "dentist", "appointment", "checkup", "health",
        "prescription", "clinic", "hospital", "medication", "medicine",
        "diagnosis", "symptom", "treatment", "therapy", "surgery",
        "nurse", "physician", "patient", "allergy", "chronic",
        "insurance", "copay", "deductible", "wellness", "vaccine",
        "blood", "lab", "xray", "mri", "specialist",
    ],
    "technical": [
        "api", "server", "database", "code", "deploy", "endpoint",
        "infrastructure", "kubernetes", "docker", "microservice",
        "repository", "commit", "branch", "pull request", "pipeline",
        "devops", "ci/cd", "terraform", "ansible", "nginx",
        "backend", "frontend", "library", "framework", "dependency",
        "sdk", "cli", "webhook", "cache", "queue", "redis",
        "postgres", "mysql", "mongodb", "elasticsearch", "kafka",
    ],
    "security": [
        "vulnerability", "cve", "exploit", "patch", "auth",
        "password", "token", "credential", "breach", "hack",
        "intrusion", "firewall", "ssl", "tls", "certificate",
        "permission", "access control", "threat", "malware",
        "phishing", "ransomware", "encryption", "2fa", "mfa",
        "audit log", "penetration", "pentest", "zero-day",
        "privilege", "secret", "api key",
    ],
    "travel": [
        "flight", "hotel", "trip", "booking", "itinerary",
        "airport", "departure", "airline", "passport", "visa",
        "reservation", "check-in", "checkout", "baggage", "luggage",
        "destination", "cruise", "train", "rental car", "airbnb",
        "vacation", "holiday", "travel", "layover", "gate",
        "boarding", "ticket", "resort", "tour", "excursion",
    ],
    "personal": [
        "birthday", "family", "mom", "dad", "friend", "prefer",
        "like", "enjoy", "hobby", "anniversary", "wedding",
        "relationship", "girlfriend", "boyfriend", "spouse", "partner",
        "child", "kid", "sibling", "brother", "sister", "parent",
        "memory", "reminder", "goal", "habit", "routine", "personal",
    ],
    "work": [
        "meeting", "sprint", "standup", "deadline", "project",
        "milestone", "client", "team", "colleague", "manager",
        "boss", "employee", "hire", "onboard", "offboard",
        "performance review", "okr", "kpi", "roadmap", "backlog",
        "velocity", "capacity", "planning", "retrospective",
        "stakeholder", "presentation", "report", "deliverable",
        "office", "remote", "wfh", "slack", "jira",
    ],
    "incident": [
        "outage", "downtime", "crash", "failure", "error",
        "incident", "broke", "down", "alert", "pagerduty",
        "postmortem", "rollback", "hotfix", "escalation", "p0",
        "sev1", "sev2", "degraded", "unavailable", "timeout",
        "exception", "stack trace", "502", "503", "500",
        "spike", "latency", "memory leak", "oom", "panic",
    ],
    "legal": [
        "contract", "nda", "agreement", "legal", "compliance",
        "audit", "regulation", "gdpr", "hipaa", "soc2",
        "terms of service", "privacy policy", "liability",
        "lawsuit", "litigation", "attorney", "lawyer", "court",
        "ip", "patent", "trademark", "copyright", "license",
        "clause", "amendment", "sign", "execute", "binding",
    ],
    "food": [
        "restaurant", "recipe", "meal", "cook", "eat",
        "diet", "food", "lunch", "dinner", "breakfast",
        "snack", "ingredient", "cuisine", "menu", "order",
        "delivery", "takeout", "grocery", "vegetarian", "vegan",
        "calorie", "nutrition", "protein", "carb", "keto",
        "gluten", "organic", "farm", "harvest", "chef",
    ],
    "fitness": [
        "gym", "workout", "exercise", "run", "yoga",
        "weights", "cardio", "swim", "cycling", "marathon",
        "training", "fitness", "crossfit", "pilates", "stretching",
        "muscle", "strength", "endurance", "reps", "sets",
        "personal trainer", "membership", "class", "studio",
    ],
    "education": [
        "course", "class", "learn", "study", "school",
        "university", "college", "degree", "certificate", "diploma",
        "lecture", "assignment", "homework", "exam", "test",
        "student", "teacher", "professor", "tutor", "curriculum",
        "textbook", "syllabus", "grade", "scholarship", "research",
    ],
    "entertainment": [
        "movie", "show", "series", "episode", "stream",
        "netflix", "spotify", "youtube", "game", "gaming",
        "concert", "music", "album", "podcast", "book",
        "novel", "theater", "comedy", "drama", "documentary",
        "actor", "director", "playlist", "subscription", "watch",
    ],
    "real_estate": [
        "apartment", "house", "condo", "rent", "lease",
        "mortgage", "property", "landlord", "tenant", "listing",
        "realtor", "zillow", "square feet", "bedroom", "bathroom",
        "neighborhood", "move in", "move out", "deposit", "utility",
        "maintenance", "repair", "hoa", "escrow", "closing",
    ],
    "communication": [
        "email", "message", "text", "call", "voicemail",
        "chat", "notify", "notification", "alert", "reminder",
        "calendar", "schedule", "invite", "rsvp", "follow up",
        "reply", "forward", "cc", "bcc", "thread", "inbox",
    ],
    "automotive": [
        "car", "vehicle", "truck", "suv", "van",
        "oil change", "tire", "brake", "engine", "transmission",
        "service", "mechanic", "dealership", "insurance", "registration",
        "parking", "garage", "gas", "fuel", "mileage",
        "lease", "finance", "down payment", "vin", "carfax",
    ],
    "shopping": [
        "order", "purchase", "buy", "bought", "cart",
        "amazon", "ebay", "etsy", "shopify", "store",
        "return", "refund", "shipping", "delivery", "tracking",
        "package", "item", "product", "brand", "review",
        "wishlist", "sale", "promo", "deal", "coupon",
    ],
    "social_media": [
        "twitter", "x.com", "instagram", "linkedin", "facebook",
        "tiktok", "reddit", "post", "tweet", "share",
        "follower", "like", "comment", "dm", "story",
        "influencer", "viral", "trending", "hashtag", "mention",
        "engagement", "reach", "impression", "profile", "bio",
    ],
    "ai_ml": [
        "machine learning", "ml", "model", "training", "inference",
        "neural network", "llm", "gpt", "claude", "gemini",
        "embedding", "vector", "dataset", "fine-tune", "prompt",
        "token", "completion", "openai", "anthropic", "hugging face",
        "rag", "agent", "langchain", "prediction", "classification",
    ],
    "productivity": [
        "todo", "to-do", "task", "checklist", "organize",
        "prioritize", "focus", "pomodoro", "time block", "notion",
        "obsidian", "evernote", "trello", "asana", "monday",
        "workflow", "process", "system", "automation", "template",
        "notes", "journal", "review", "plan", "weekly",
    ],
    "devops": [
        "ci", "cd", "pipeline", "build", "test",
        "deploy", "release", "rollout", "canary", "blue-green",
        "monitoring", "metrics", "logging", "tracing", "observability",
        "grafana", "prometheus", "datadog", "sentry", "pagerduty",
        "helm", "kubectl", "terraform", "cloudformation", "ansible",
    ],
}

# Patterns that use regex (prefixed with "re:" internally)
_REGEX_PATTERNS: Dict[str, List[re.Pattern]] = {}

# Precompile patterns at module load time for performance
for _cat, _keywords in _CATEGORY_RULES.items():
    _compiled = []
    for _kw in _keywords:
        if _kw.startswith(r"\$") or _kw == r"\$":
            _compiled.append(re.compile(re.escape("$"), re.IGNORECASE))
        else:
            # Escape for literal matching
            _compiled.append(re.compile(re.escape(_kw), re.IGNORECASE))
    _REGEX_PATTERNS[_cat] = _compiled


class CategoryTagger:
    """Keyword-rule-based category tagger.

    Tags text with one or more semantic category labels based on
    keyword presence. Rules are case-insensitive substring matches.
    No external dependencies - pure stdlib.

    Attributes:
        rules: Dict mapping category name -> list of compiled regex patterns
    """

    def __init__(self):
        self.rules: Dict[str, List[re.Pattern]] = _REGEX_PATTERNS

    def _count_matches(self, text: str) -> Dict[str, int]:
        """Return match count per category for the given text."""
        counts: Dict[str, int] = {}
        for category, patterns in self.rules.items():
            hits = sum(1 for p in patterns if p.search(text))
            if hits > 0:
                counts[category] = hits
        return counts

    def tag(self, text: str) -> List[str]:
        """Return all categories that match the text.

        Args:
            text: Any string - memory content, query, etc.

        Returns:
            List of matched category names (may be empty).
            Sorted by match count descending for determinism.
        """
        counts = self._count_matches(text)
        if not counts:
            return []
        # Sort by hit count descending, then alphabetically for ties
        return [cat for cat, _ in sorted(
            counts.items(), key=lambda kv: (-kv[1], kv[0])
        )]

    def primary_category(self, text: str) -> str:
        """Return the single most-matched category, or "general".

        Args:
            text: Input text to classify.

        Returns:
            Category name string, or "general" if no keywords matched.
        """
        counts = self._count_matches(text)
        if not counts:
            return "general"
        # Return the category with the highest match count
        return max(counts, key=lambda cat: (counts[cat], cat))

    def tag_with_counts(self, text: str) -> Dict[str, int]:
        """Return match counts per category (useful for debugging).

        Args:
            text: Input text.

        Returns:
            Dict of {category: hit_count} for all matched categories.
        """
        return self._count_matches(text)
