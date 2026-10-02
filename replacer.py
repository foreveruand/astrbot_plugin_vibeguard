import re
from typing import NamedTuple

from .token_cache import TokenCache


class MatchSpan(NamedTuple):
    start: int
    end: int
    matched_text: str
    category: str


class SensitiveReplacer:
    """Detects sensitive terms/patterns, manages replacements, and restores original text."""

    def __init__(
        self,
        sensitive_words: list[str] | None,
        sensitive_patterns: list[str] | list[dict] | None,
        cache: TokenCache,
    ) -> None:
        """Initialize SensitiveReplacer.

        Args:
            sensitive_words: List of literal sensitive strings to match.
            sensitive_patterns: List of regex strings or dicts with 'pattern' and 'category'.
            cache: TokenCache instance for managing tokens and TTLs.
        """
        self.cache = cache
        self.sensitive_words: list[str] = [
            w for w in (sensitive_words or []) if isinstance(w, str) and w.strip()
        ]
        # Sort words descending by length to ensure longer phrases match before substrings
        self.sensitive_words.sort(key=len, reverse=True)

        self.compiled_patterns: list[tuple[re.Pattern, str]] = []
        for p in sensitive_patterns or []:
            pattern_str = ""
            category = "REGEX"
            if isinstance(p, dict):
                pattern_str = p.get("pattern", "")
                category = p.get("category", "REGEX")
            elif isinstance(p, str):
                pattern_str = p
                # Infer human-friendly category if possible
                upper_p = pattern_str.upper()
                if "KEY" in upper_p or "SK-" in upper_p:
                    category = "KEY"
                elif "TOKEN" in upper_p:
                    category = "TOKEN"
                elif "\\D{11}" in pattern_str or "PHONE" in upper_p:
                    category = "PHONE"
                elif "@" in pattern_str or "EMAIL" in upper_p:
                    category = "EMAIL"

            if pattern_str:
                try:
                    compiled = re.compile(pattern_str)
                    self.compiled_patterns.append((compiled, category))
                except re.error:
                    pass

    async def replace_text(self, text: str) -> str:
        """Scan text, collect non-overlapping sensitive spans, replace with placeholders.

        Args:
            text: Input text containing sensitive words.

        Returns:
            Text with sensitive words replaced by safe placeholders.
        """
        if not text:
            return text

        spans: list[MatchSpan] = []

        # 1. Collect regex pattern matches
        for pattern, category in self.compiled_patterns:
            for match in pattern.finditer(text):
                matched = match.group(0)
                if matched:
                    spans.append(
                        MatchSpan(
                            start=match.start(),
                            end=match.end(),
                            matched_text=matched,
                            category=category,
                        )
                    )

        # 2. Collect literal sensitive word matches
        for word in self.sensitive_words:
            start = 0
            while True:
                idx = text.find(word, start)
                if idx == -1:
                    break
                spans.append(
                    MatchSpan(
                        start=idx,
                        end=idx + len(word),
                        matched_text=word,
                        category="WORD",
                    )
                )
                start = idx + len(word)

        if not spans:
            return text

        # Sort spans by start asc, then length desc (longest match first)
        spans.sort(key=lambda s: (s.start, -(s.end - s.start)))

        # Resolve overlapping spans (greedy left-to-right selection)
        non_overlapping: list[MatchSpan] = []
        last_end = 0
        for span in spans:
            if span.start >= last_end:
                non_overlapping.append(span)
                last_end = span.end

        if not non_overlapping:
            return text

        # Perform replacement from right to left to preserve offsets
        result_chars = list(text)
        for span in reversed(non_overlapping):
            placeholder = await self.cache.get_or_create(
                span.matched_text, category=span.category
            )
            result_chars[span.start : span.end] = list(placeholder)

        return "".join(result_chars)

    async def restore_text(self, text: str) -> str:
        """Restore placeholders in text back to original values.

        Args:
            text: Text with placeholders.

        Returns:
            Restored text.
        """
        return await self.cache.restore_text(text)
