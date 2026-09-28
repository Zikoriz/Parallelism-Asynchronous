import asyncio
import logging

from url_filter import host_of

logger = logging.getLogger(__name__)

# refill arithmetic leaves e.g. 0.9999999999999998 tokens after sleeping exactly the
# computed time; without a tolerance the limiter would sleep again for ~1e-17 s
_EPSILON = 1e-9


class RateLimiter:
    """Token bucket: at most `requests_per_second` grants per second on average.

    The bucket holds up to `burst` tokens and refills continuously. With the default
    burst=1 grants are spaced by exactly 1/requests_per_second (5 rps -> every 200 ms);
    a larger burst lets that many requests go out back to back after an idle period.
    requests_per_second=None (or 0) disables the limit.

    Waiters are served in FIFO order (asyncio.Lock is fair), and time comes from
    loop.time(), so tests can drive it with a virtual clock.
    """

    def __init__(self, requests_per_second: float | None, burst: int = 1):
        if requests_per_second is not None and requests_per_second < 0:
            raise ValueError("requests_per_second must be >= 0")
        if burst < 1:
            raise ValueError("burst must be >= 1")
        self.rate = requests_per_second or None
        self.burst = burst
        self._tokens = float(burst)
        self._updated: float | None = None
        self._lock = asyncio.Lock()
        # metric: grant timestamps (loop.time()) of the first and last request
        self.count = 0
        self.first_at: float | None = None
        self.last_at: float | None = None

    @property
    def interval(self) -> float:
        return 1 / self.rate if self.rate else 0.0

    async def acquire(self) -> None:
        """Wait until a request may be sent, then take the token."""
        async with self._lock:
            await self._wait_token()
            self._take()

    async def __aenter__(self) -> "RateLimiter":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        pass  # tokens are spent, not returned

    def actual_rps(self) -> float | None:
        """Measured rate between the first and the last grant; None until two grants happened."""
        if self.count < 2 or self.last_at == self.first_at:
            return None
        return (self.count - 1) / (self.last_at - self.first_at)

    # -- internals, also used by DomainRateLimiter ---------------------------

    def _refill(self, now: float) -> None:
        if self._updated is not None and self.rate:
            self._tokens = min(self.burst, self._tokens + (now - self._updated) * self.rate)
        self._updated = now

    async def _wait_token(self) -> None:
        """Sleep until a whole token is available (without taking it). Caller holds _lock."""
        if not self.rate:
            return
        loop = asyncio.get_running_loop()
        while True:
            self._refill(loop.time())
            if self._tokens >= 1 - _EPSILON:
                return
            # loop.call_at may fire up to clock_resolution early: the loop re-checks
            await asyncio.sleep((1 - self._tokens) / self.rate)

    def _take(self) -> None:
        now = asyncio.get_running_loop().time()
        if self.rate:
            self._refill(now)
            self._tokens = max(0.0, self._tokens - 1)
        self.count += 1
        if self.first_at is None:
            self.first_at = now
        self.last_at = now


class DomainRateLimiter:
    """Global RateLimiter + one RateLimiter per host (`per_host_rate_limiter`).

    acquire(url) returns once both limits allow the request. The host token is taken
    only *after* the global one is granted, while holding the host's lock, so the
    moment a request leaves is spaced correctly for its host even when it had to
    queue for the global limit. A slow (low-rate) host only makes its own requests
    wait: other hosts have independent buckets.
    """

    def __init__(self, requests_per_second: float | None = None, per_host_rps: float | None = None, burst: int = 1):
        self.global_limiter = RateLimiter(requests_per_second, burst)
        self.per_host_rps = per_host_rps
        self.burst = burst
        self.per_host_rate_limiter: dict[str, RateLimiter] = {}

    def for_host(self, host: str) -> RateLimiter:
        limiter = self.per_host_rate_limiter.get(host)
        if limiter is None:
            limiter = self.per_host_rate_limiter[host] = RateLimiter(self.per_host_rps, self.burst)
        return limiter

    async def acquire(self, url: str) -> None:
        host = self.for_host(host_of(url))
        async with host._lock:
            await host._wait_token()
            await self.global_limiter.acquire()
            host._take()

    def actual_rps(self) -> dict:
        """{"global": rps, "per_host": {host: rps}} measured so far (None = not enough data)."""
        return {
            "global": self.global_limiter.actual_rps(),
            "per_host": {h: lim.actual_rps() for h, lim in self.per_host_rate_limiter.items()},
        }

    def check_limits(self, tolerance: float = 0.10) -> list[str]:
        """Human-readable violations: measured rate above the configured one by more than tolerance."""
        problems = []
        pairs = [("global", self.global_limiter)] + list(self.per_host_rate_limiter.items())
        for name, limiter in pairs:
            rps = limiter.actual_rps()
            if limiter.rate and rps is not None and rps > limiter.rate * (1 + tolerance):
                problems.append(f"{name}: {rps:.2f} rps > limit {limiter.rate:g}")
        return problems
