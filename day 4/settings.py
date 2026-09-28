from dataclasses import dataclass

from day3 import Config as BaseConfig


@dataclass(frozen=True)
class Config(BaseConfig):
    """Day 1 Config + retry settings.

    Timeouts: total/connect/read_timeout (inherited) bound one attempt through
    aiohttp.ClientTimeout; operation_timeout bounds the whole fetch of a URL,
    all retries and backoff pauses included (None = no bound).

    Backoff before retry n (1-based): min(backoff_max, backoff_base * backoff_factor**(n-1)),
    stretched by a random factor in [1, 1 + backoff_jitter]. A Retry-After from the
    server (429/503) replaces it, capped at max_retry_after.
    """

    user_agent: str = "AsyncCrawler/0.4"
    max_retries: int = 3  # retries after the first attempt: up to 1 + max_retries requests
    backoff_base: float = 0.5
    backoff_factor: float = 2.0
    backoff_max: float = 30.0
    backoff_jitter: float = 0.5
    max_retry_after: float = 60.0
    operation_timeout: float | None = 120.0
