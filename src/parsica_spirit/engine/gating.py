"""
Input Gating - P0-P3 priority classification for memory intake.

Automatically classifies content by importance:
- P0: Critical (security, errors, financial commitments, deadlines)
- P1: Operational (decisions, assignments, technical choices, meeting outcomes)
- P2: Contextual (background info, general discussion, supplementary context)
- P3: Ephemeral (greetings, small talk, filler)

P3 content is typically filtered out to prevent noise pollution.
"""

import re
from typing import Dict, Optional


class InputGate:
    """Intelligent content triage system for memory intake."""

    def __init__(self):
        # P0: Critical signals
        self._p0_patterns = [
            # Security and errors
            r'(?:security|vulnerability|breach|attack|error|exception|failure|crash)',
            r'(?:unauthorized|malicious|threat|risk|critical|emergency)',
            r'(?:password|token|key|secret|credential).*(?:compromised|leaked|exposed)',
            
            # Financial commitments
            r'\$[\d,]+(?:\.\d{2})?.*(?:committed?|approved?|agreed?|contracted?|project)',
            r'(?:budget|payment|invoice|billing).*(?:due|overdue|critical|approved?)',
            r'(?:legal|contract|agreement|liability|lawsuit)',
            
            # Deadlines and time-critical
            r'(?:deadline|due.*date|urgent|asap|immediately)',
            r'(?:expires?|timeout|cutoff).*(?:today|tomorrow|this week)',
        ]
        
        # P1: Operational signals  
        self._p1_patterns = [
            # Decisions and assignments
            r'(?:decided?|chosen|selected|assigned|delegated)',
            r'(?:approved?|rejected|implemented|deployed)',
            r'(?:action.*item|task.*assigned|responsibility)',
            
            # Technical choices
            r'(?:technology|architecture|database|framework|library).*(?:choice|decision)',
            r'(?:api|service|integration|deployment|configuration)',
            
            # Meeting outcomes
            r'(?:meeting|discussion|call).*(?:outcome|result|conclusion)',
            r'(?:agreed|consensus|next.*step|follow.*up)',
        ]
        
        # P2: Contextual signals
        self._p2_patterns = [
            # Background information
            r'(?:background|context|history|explanation)',
            r'(?:research|investigation|analysis|findings)',
            r'(?:documentation|specification|requirements)',
            r'(?:for.*reference|fyi|note|information)',
        ]
        
        # P3: Ephemeral signals - explicit patterns for noise
        self._p3_patterns = [
            # Greetings and social
            r'^(?:hi|hey|hello|good\s+(?:morning|afternoon|evening))',
            r'^(?:thanks?(?:\s+you)?|thx|appreciate|cheers)\.?$',
            r'^thanks?\s+for\s+(?:the|your)\s+\w+\.?$',  # "Thanks for the update", etc.
            r'^(?:ok|okay|got\s+it|understood|copy|noted?)\.?$',
            r'^(?:lol|haha|lmao|nice|cool|awesome|great)(?:\s+that\'?s\s+\w+)?\.?$',
            r'^(?:bye|see\s+you|talk\s+(?:later|soon)|ttyl)\.?$',
            
            # Acknowledgments and filler
            r'^(?:yep|yeah|yup|nope|no\s+problem)\.?$',
            r'^(?:sounds?\s+good|works?\s+for\s+me|agreed?)\.?$',
            r'^(?:will\s+do|on\s+it|got\s+it)\.?$',
            r'^(?:that\'?s\s+(?:funny|great|nice|cool))\.?$',
            
            # Very short low-content messages
            r'^.{1,3}$',  # Single chars, emoticons, etc.
        ]

    def classify(self, content: str, context: Dict = None) -> str:
        """Classify content priority level: P0, P1, P2, or P3.
        
        Args:
            content: Text content to classify
            context: Optional context hints (source, category, etc.)
            
        Returns:
            Priority level as string: "P0", "P1", "P2", or "P3"
        """
        if not content or len(content.strip()) < 3:
            return "P3"
            
        text = content.strip().lower()
        
        # Check context hints first
        if context:
            source = context.get('source', '').lower()
            category = context.get('category', '').lower()
            
            # Source-based routing
            if any(term in source for term in ['security', 'alert', 'error', 'critical']):
                return "P0"
            if any(term in source for term in ['meeting', 'decision', 'technical']):
                return "P1"
                
            # Category-based routing
            if category in ['strategic', 'critical']:
                return "P0"
            elif category in ['operational', 'business']:
                return "P1"
            elif category in ['tactical', 'technical']:
                return "P2"
        
        # Pattern-based classification
        for pattern in self._p0_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return "P0"
                
        for pattern in self._p1_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return "P1"
                
        # P3 patterns need to match more precisely
        for pattern in self._p3_patterns:
            if re.match(pattern, text, re.IGNORECASE):
                return "P3"
        
        for pattern in self._p2_patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return "P2"
                
        # Default classification based on length and complexity
        if len(text) < 15:
            return "P3"
        elif any(word in text for word in ['project', 'system', 'data', 'code', 'feature']):
            return "P2"
        else:
            return "P2"  # Default to contextual

    def should_store(self, content: str, context: Dict = None) -> bool:
        """Determine if content should be stored in memory.
        
        Args:
            content: Text content to evaluate
            context: Optional context hints
            
        Returns:
            True if content should be stored, False for P3 ephemeral content
        """
        priority = self.classify(content, context)
        return priority != "P3"

    def route(self, content: str, context: Dict = None) -> Dict:
        """Complete routing decision for content.
        
        Args:
            content: Text content to route
            context: Optional context hints
            
        Returns:
            Dict with priority, category, and store decision
        """
        priority = self.classify(content, context)
        
        # Map priority to category
        category_map = {
            "P0": "strategic",
            "P1": "operational", 
            "P2": "tactical",
            "P3": "ephemeral"
        }
        
        return {
            "priority": priority,
            "category": category_map[priority],
            "store": self.should_store(content, context)
        }