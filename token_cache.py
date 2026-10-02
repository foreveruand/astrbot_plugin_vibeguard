import asyncio
import secrets
import time
from dataclasses import dataclass


@dataclass
class CacheEntry:
    """Represents a cached token mapping entry.

    Attributes:
        placeholder: The generated safe replacement token.
        original: The original sensitive string.
        category: Category tag for readability (e.g., 'SECRET', 'KEY').
        expires_at: Epoch timestamp after which this entry expires.
    """

    placeholder: str
    original: str
    category: str
    expires_at: float


class TokenCache:
    """Thread-safe cache that maintains bidirectional mappings between sensitive strings and placeholders.

    Features:
    - Deterministic within TTL: Same sensitive value maps to the same placeholder during TTL.
    - Sliding window TTL: Accessing or refreshing an active mapping extends its expiration.
    - Expiration renewal: Once expired, a new unique random placeholder is issued.
    """

    def __init__(
        self,
        ttl_seconds: int = 3600,
        prefix: str = "__VG_",
        suffix: str = "__",
    ) -> None:
        """Initialize TokenCache.

        Args:
            ttl_seconds: Expiration window in seconds (default: 3600s / 1h).
            prefix: Placeholder prefix string.
            suffix: Placeholder suffix string.
        """
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.prefix = prefix
        self.suffix = suffix
        self._lock = asyncio.Lock()
        # original -> CacheEntry
        self._forward: dict[str, CacheEntry] = {}
        # placeholder -> original
        self._reverse: dict[str, str] = {}

    def _generate_placeholder(self, category: str) -> str:
        """Generate a random placeholder token avoiding collisions.

        Args:
            category: Category name tag.

        Returns:
            Unique placeholder token.
        """
        cat_clean = "".join(c for c in category.upper() if c.isalnum()) or "SECRET"
        while True:
            rand_hex = secrets.token_hex(4)
            token = f"{self.prefix}{cat_clean}_{rand_hex}{self.suffix}"
            if token not in self._reverse:
                return token

    async def get_or_create(self, original: str, category: str = "SECRET") -> str:
        """Retrieve an active placeholder or generate a new one, refreshing TTL.

        Args:
            original: The sensitive string to replace.
            category: Semantic category label.

        Returns:
            The safe placeholder string.
        """
        if not original:
            return original

        now = time.time()
        async with self._lock:
            entry = self._forward.get(original)
            if entry is not None and entry.expires_at > now:
                # Active: refresh expiration (sliding TTL window)
                entry.expires_at = now + self.ttl_seconds
                return entry.placeholder

            # Expired or new: remove stale reverse mapping if expired entry existed
            if entry is not None:
                self._reverse.pop(entry.placeholder, None)

            # Generate new placeholder and store
            placeholder = self._generate_placeholder(category)
            new_entry = CacheEntry(
                placeholder=placeholder,
                original=original,
                category=category,
                expires_at=now + self.ttl_seconds,
            )
            self._forward[original] = new_entry
            self._reverse[placeholder] = original
            return placeholder

    async def get_original(self, placeholder: str) -> str | None:
        """Get original string for a placeholder.

        Args:
            placeholder: The placeholder token.

        Returns:
            Original sensitive string if found, None otherwise.
        """
        async with self._lock:
            return self._reverse.get(placeholder)

    async def restore_text(self, text: str) -> str:
        """Replace all known active or recently tracked placeholders back to their originals.

        Args:
            text: Text potentially containing placeholders.

        Returns:
            Restored text with original values.
        """
        if not text or self.prefix not in text:
            return text

        async with self._lock:
            # Copy mapping for safe string iteration
            reverse_map = dict(self._reverse)

        for placeholder, original in reverse_map.items():
            if placeholder in text:
                text = text.replace(placeholder, original)
        return text

    async def cleanup_expired(self) -> int:
        """Remove entries that have expired.

        Returns:
            Number of evicted entries.
        """
        now = time.time()
        evicted = 0
        async with self._lock:
            expired_keys = [k for k, v in self._forward.items() if v.expires_at <= now]
            for k in expired_keys:
                entry = self._forward.pop(k, None)
                if entry:
                    self._reverse.pop(entry.placeholder, None)
                    evicted += 1
        return evicted

    def clear(self) -> None:
        """Synchronously clear all cache mappings."""
        self._forward.clear()
        self._reverse.clear()
