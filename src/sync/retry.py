"""Retry utilities with exponential backoff."""

import logging
import random
import time
from dataclasses import dataclass
from typing import Callable, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class RetryConfig:
    """Configuration for retry behavior."""

    max_retries: int = 3
    base_delay: float = 1.0  # seconds
    max_delay: float = 60.0  # seconds
    exponential_base: float = 2.0
    jitter: bool = True  # Add randomness to prevent thundering herd


class RetryExhausted(Exception):
    """All retry attempts exhausted."""

    def __init__(self, attempts: int, last_error: Optional[Exception] = None):
        self.attempts = attempts
        self.last_error = last_error
        super().__init__(f"Retry exhausted after {attempts} attempts")


def calculate_delay(
    attempt: int,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    jitter: bool = True,
) -> float:
    """Calculate delay for a retry attempt with exponential backoff.

    Args:
        attempt: Current attempt number (0-indexed)
        base_delay: Initial delay in seconds
        max_delay: Maximum delay cap
        exponential_base: Base for exponential calculation
        jitter: Whether to add random jitter

    Returns:
        Delay in seconds
    """
    delay = base_delay * (exponential_base ** attempt)
    delay = min(delay, max_delay)

    if jitter:
        # Add +/- 25% jitter
        jitter_range = delay * 0.25
        delay = delay + random.uniform(-jitter_range, jitter_range)

    return max(0, delay)


def retry_with_backoff(
    func: Callable[[], T],
    config: Optional[RetryConfig] = None,
    on_retry: Optional[Callable[[int, Exception, float], None]] = None,
    retryable_exceptions: tuple = (Exception,),
) -> T:
    """Execute a function with exponential backoff retry.

    Args:
        func: Function to execute
        config: Retry configuration
        on_retry: Callback called before each retry (attempt, error, delay)
        retryable_exceptions: Tuple of exceptions that should trigger retry

    Returns:
        Result of successful function call

    Raises:
        RetryExhausted: If all retries fail
        Exception: If a non-retryable exception occurs
    """
    if config is None:
        config = RetryConfig()

    last_error: Optional[Exception] = None

    for attempt in range(config.max_retries + 1):
        try:
            return func()
        except retryable_exceptions as e:
            last_error = e

            if attempt >= config.max_retries:
                break

            delay = calculate_delay(
                attempt,
                config.base_delay,
                config.max_delay,
                config.exponential_base,
                config.jitter,
            )

            if on_retry:
                on_retry(attempt, e, delay)
            else:
                logger.warning(
                    f"Attempt {attempt + 1} failed: {e}. "
                    f"Retrying in {delay:.1f}s..."
                )

            time.sleep(delay)

    raise RetryExhausted(config.max_retries + 1, last_error)


class NetworkReachabilityCache:
    """Caches network reachability status to avoid excessive checks."""

    def __init__(self, ttl_seconds: float = 30.0):
        """Initialize cache.

        Args:
            ttl_seconds: How long to cache reachability status
        """
        self.ttl = ttl_seconds
        self._cache: dict[str, tuple[bool, float]] = {}

    def get(self, key: str) -> Optional[bool]:
        """Get cached reachability status.

        Args:
            key: Cache key (e.g., hostname)

        Returns:
            Cached status, or None if expired/missing
        """
        if key not in self._cache:
            return None

        status, timestamp = self._cache[key]
        if time.time() - timestamp > self.ttl:
            del self._cache[key]
            return None

        return status

    def set(self, key: str, status: bool) -> None:
        """Cache reachability status.

        Args:
            key: Cache key
            status: Reachability status
        """
        self._cache[key] = (status, time.time())

    def invalidate(self, key: Optional[str] = None) -> None:
        """Invalidate cache.

        Args:
            key: Specific key to invalidate, or None for all
        """
        if key:
            self._cache.pop(key, None)
        else:
            self._cache.clear()
