"""Retry policy helpers: exponential backoff with jitter and Retry-After parsing."""
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from settings import Config

RETRYABLE_STATUSES = frozenset({429}) | frozenset(range(500, 600))


def is_retryable_status(status: int) -> bool:
    """5xx and 429 are temporary; any other 4xx will not change on a retry."""
    return status in RETRYABLE_STATUSES


def backoff_delay(retry: int, config: Config, rng: random.Random | None = None) -> float:
    """Pause before retry number `retry` (1 = first retry).

    base * factor**(retry-1), capped at backoff_max, then multiplied by a random
    factor in [1, 1 + backoff_jitter] so that many clients failing at once don't
    retry in lockstep.
    """
    raw = min(config.backoff_max, config.backoff_base * config.backoff_factor ** (retry - 1))
    if config.backoff_jitter:
        raw *= 1 + (rng or random).uniform(0, config.backoff_jitter)
    return raw


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Retry-After header -> seconds to wait; accepts delta-seconds or an HTTP date.

    None for a missing or malformed header; a date in the past gives 0.
    """
    if value is None or not value.strip():
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0.0, (when - now).total_seconds())
