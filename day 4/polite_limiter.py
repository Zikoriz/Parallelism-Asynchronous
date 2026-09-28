"""Politeness rate limiter: per-domain (or global) request spacing, min_delay, jitter,
robots.txt Crawl-delay, server-requested pauses and delay/speed monitoring."""
import asyncio
import random
from collections import deque
from dataclasses import dataclass

_EPSILON = 1e-9
GLOBAL_KEY = "*"


@dataclass
class _DomainStats:
    requests: int = 0
    total_wait: float = 0.0
    total_gap: float = 0.0
    last_at: float | None = None

    def record(self, at: float, waited: float) -> None:
        if self.last_at is not None:
            self.total_gap += at - self.last_at
        self.requests += 1
        self.total_wait += waited
        self.last_at = at

    def as_dict(self) -> dict:
        return {
            "requests": self.requests,
            "avg_wait": self.total_wait / self.requests if self.requests else 0.0,
            "avg_interval": self.total_gap / (self.requests - 1) if self.requests > 1 else None,
        }


class RateMonitor:
    """Request speed and delay statistics, on loop.time().

    - current_rps: rate among the requests of the last `window` seconds;
    - average_rps: rate between the first and the last request;
    - avg_interval: mean gap between consecutive requests (overall and per domain);
    - avg_wait: mean time a request waited in RateLimiter.acquire().
    """

    def __init__(self, window: float = 10.0):
        self.window = window
        self.total = _DomainStats()
        self.per_domain: dict[str, _DomainStats] = {}
        self.first_at: float | None = None
        self._recent: deque[float] = deque()

    def record(self, domain: str, at: float, waited: float) -> None:
        if self.first_at is None:
            self.first_at = at
        self.total.record(at, waited)
        self.per_domain.setdefault(domain, _DomainStats()).record(at, waited)
        self._recent.append(at)

    def _now(self) -> float:
        try:
            return asyncio.get_running_loop().time()
        except RuntimeError:  # stats read after the crawl, outside the loop
            return self.total.last_at or 0.0

    def current_rps(self, now: float | None = None) -> float:
        now = self._now() if now is None else now
        while self._recent and self._recent[0] <= now - self.window:
            self._recent.popleft()
        if len(self._recent) < 2 or self._recent[-1] == self._recent[0]:
            return 0.0
        return (len(self._recent) - 1) / (self._recent[-1] - self._recent[0])

    def average_rps(self) -> float:
        t = self.total
        if t.requests < 2 or t.last_at == self.first_at:
            return 0.0
        return (t.requests - 1) / (t.last_at - self.first_at)

    def get_stats(self, now: float | None = None) -> dict:
        return {
            **self.total.as_dict(),
            "current_rps": self.current_rps(now),
            "average_rps": self.average_rps(),
            "per_domain": {d: s.as_dict() for d, s in self.per_domain.items()},
        }


class RateLimiter:
    """Waits before each request so that requests are spaced politely.

    - per_domain=True: every domain has its own schedule (a slow site doesn't
      delay others); per_domain=False: one global schedule for all requests.
    - The gap after a request is max(1/requests_per_second, min_delay, Crawl-delay
      of its domain) plus a random jitter in [0, jitter] seconds.
    - pause(domain, seconds): nobody sends to that domain (or at all, in global
      mode) before the pause ends — used for 429/503 Retry-After.
    - requests_per_second=None (or 0) removes the rate part; min_delay still applies.

    Waiters of one schedule are served in FIFO order (asyncio.Lock is fair).
    """

    def __init__(
        self,
        requests_per_second: float | None = 1.0,
        per_domain: bool = True,
        *,
        min_delay: float = 0.0,
        jitter: float = 0.0,
        rng: random.Random | None = None,
        monitor_window: float = 10.0,
    ):
        if requests_per_second is not None and requests_per_second < 0:
            raise ValueError("requests_per_second must be >= 0")
        if min_delay < 0 or jitter < 0:
            raise ValueError("min_delay and jitter must be >= 0")
        self.requests_per_second = requests_per_second or None
        self.per_domain = per_domain
        self.min_delay = min_delay
        self.jitter = jitter
        self.rng = rng or random.Random()
        self.crawl_delays: dict[str, float] = {}
        self.monitor = RateMonitor(monitor_window)
        self._next_at: dict[str, float] = {}
        self._last_at: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def base_interval(self) -> float:
        rate_gap = 1 / self.requests_per_second if self.requests_per_second else 0.0
        return max(rate_gap, self.min_delay)

    def interval_for(self, domain: str | None) -> float:
        """Gap after a request to `domain`, without jitter."""
        return max(self.base_interval, self.crawl_delays.get(domain or GLOBAL_KEY, 0.0))

    def set_crawl_delay(self, domain: str, delay: float | None) -> None:
        """Crawl-delay from robots.txt; it only ever raises the domain's interval.

        The request already scheduled is pushed back as well: robots.txt itself was
        fetched before its Crawl-delay was known, and the next page must wait for it.
        """
        if delay:
            self.crawl_delays[domain] = delay
            key = self._key(domain)
            if key in self._last_at:
                self._next_at[key] = max(self._next_at[key], self._last_at[key] + delay)
        else:
            self.crawl_delays.pop(domain, None)

    def _key(self, domain: str | None) -> str:
        return (domain or GLOBAL_KEY) if self.per_domain else GLOBAL_KEY

    async def acquire(self, domain: str | None = None) -> float:
        """Wait until a request to `domain` may be sent; returns the time waited."""
        key = self._key(domain)
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        loop = asyncio.get_running_loop()
        requested = loop.time()
        async with lock:
            # a loop, not one sleep: pause() may move the schedule while we sleep
            while (wait := self._next_at.get(key, 0.0) - loop.time()) > _EPSILON:
                await asyncio.sleep(wait)
            now = loop.time()
            gap = self.interval_for(domain) + (self.rng.uniform(0, self.jitter) if self.jitter else 0.0)
            self._next_at[key] = now + gap
            self._last_at[key] = now
            waited = now - requested
            self.monitor.record(domain or GLOBAL_KEY, now, waited)
            return waited

    def pause(self, domain: str | None, seconds: float) -> None:
        """Push the domain's (in global mode: everyone's) next request at least `seconds` from now."""
        key = self._key(domain)
        until = asyncio.get_running_loop().time() + seconds
        self._next_at[key] = max(self._next_at.get(key, 0.0), until)

    def get_stats(self) -> dict:
        stats = self.monitor.get_stats()
        stats["crawl_delays"] = dict(self.crawl_delays)
        stats["configured_interval"] = self.base_interval
        return stats
