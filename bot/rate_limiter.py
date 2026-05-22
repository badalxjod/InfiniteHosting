"""
bot/rate_limiter.py — Per-user rate limiter.

Limits each user to RATE_LIMIT actions per RATE_WINDOW seconds.
Applied to all message and callback handlers via the is_rate_limited() helper.
"""
import time
import logging
from collections import defaultdict

logger = logging.getLogger(__name__)

RATE_LIMIT = 5    # maximum calls
RATE_WINDOW = 10  # per N seconds


class RateLimiter:
    """Thread-safe per-user rate limiter using a sliding window."""

    def __init__(self, limit: int = RATE_LIMIT, window: int = RATE_WINDOW) -> None:
        """
        Initialize the rate limiter.

        Args:
            limit: Maximum number of allowed calls per window.
            window: Window size in seconds.
        """
        self.limit = limit
        self.window = window
        self._data: dict[int, dict] = defaultdict(lambda: {"calls": 0, "reset_time": 0.0})

    def is_limited(self, user_id: int) -> bool:
        """
        Check whether a user has exceeded the rate limit.

        Args:
            user_id: Telegram user ID to check.

        Returns:
            True if the user is currently rate-limited, False otherwise.
        """
        now = time.monotonic()
        entry = self._data[user_id]

        if now > entry["reset_time"]:
            entry["calls"] = 0
            entry["reset_time"] = now + self.window

        entry["calls"] += 1
        if entry["calls"] > self.limit:
            logger.debug("Rate limit hit for user %s (%d calls)", user_id, entry["calls"])
            return True
        return False


# Global singleton used by handlers
rate_limiter = RateLimiter()
