"""Emotional / sentiment tagging for memories."""

from typing import Dict, Optional

SENTIMENT_KEYWORDS: Dict[str, list] = {
    "positive": [
        "achieved", "breakthrough", "complete", "success", "excellent", "great",
        "perfect", "working", "stable", "solved", "fixed", "improved", "love",
        "amazing", "brilliant", "excited", "happy", "proud", "winning", "profit",
        "✅", "🎉", "🚀", "💪", "🏆", "❤️", "😊", "👍",
    ],
    "negative": [
        "failed", "broken", "crashed", "error", "bug", "lost", "frustrated",
        "stuck", "blocked", "problem", "issue", "concern", "worried", "urgent",
        "critical", "expensive", "overbudget", "delayed", "missing",
        "❌", "🔴", "😤", "😰", "💀",
    ],
    "urgent": [
        "urgent", "asap", "immediately", "critical", "deadline",
        "tonight", "right now", "emergency", "blocking", "must", "need",
        "⚠️", "🚨", "⏰",
    ],
    "strategic": [
        "decision", "strategy", "plan", "approach", "architecture", "design",
        "pivot", "direction", "vision", "goal", "milestone", "roadmap",
        "🎯", "📋", "🗺️",
    ],
    "financial": [
        "cost", "revenue", "profit", "savings", "budget", "price", "fee",
        "invoice", "payment", "grant", "funding", "investment",
        "💰", "💵", "📈", "📉",
    ],
}


class SentimentTagger:
    """Lightweight keyword-based sentiment analysis.

    No external dependencies. Upgrade path: swap in a transformer model
    or OpenAI call for production-grade analysis.
    """

    def __init__(self, keywords: Dict[str, list] = None):
        self.keywords = keywords or SENTIMENT_KEYWORDS

    def analyze(self, text: str) -> Dict[str, float]:
        """Return {label: score} for each detected sentiment."""
        text_lower = text.lower()
        scores: Dict[str, float] = {}
        for label, kws in self.keywords.items():
            hits = sum(1 for kw in kws if kw.lower() in text_lower)
            if hits:
                scores[label] = round(min(hits / 3.0, 1.0), 2)
        return scores

    @staticmethod
    def dominant(scores: Dict[str, float]) -> Optional[str]:
        """Return the strongest sentiment label, or None."""
        return max(scores, key=scores.get) if scores else None
