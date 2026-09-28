import asyncio
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from url_filter import host_of


class SemaphoreManager:
    """Global concurrency limit + a per-domain limit, with active-task tracking.

    `async with manager.slot(url): ...` waits for a free slot of the URL's domain first
    and only then for a global slot, so requests queued behind a busy domain don't
    occupy global slots other domains could use.
    """

    def __init__(self, max_concurrent: int = 10, max_per_domain: int | None = None):
        if max_concurrent < 1 or (max_per_domain is not None and max_per_domain < 1):
            raise ValueError("limits must be >= 1")
        self.max_concurrent = max_concurrent
        self.max_per_domain = max_per_domain or max_concurrent
        self._global = asyncio.Semaphore(max_concurrent)
        self._domains: dict[str, asyncio.Semaphore] = {}
        self.active_per_domain: Counter[str] = Counter()
        self.peak_active = 0
        self.peak_per_domain: Counter[str] = Counter()

    def _domain_semaphore(self, domain: str) -> asyncio.Semaphore:
        sem = self._domains.get(domain)
        if sem is None:
            sem = self._domains[domain] = asyncio.Semaphore(self.max_per_domain)
        return sem

    @asynccontextmanager
    async def slot(self, url: str) -> AsyncIterator[None]:
        domain = host_of(url)
        async with self._domain_semaphore(domain), self._global:
            self.active_per_domain[domain] += 1
            self.peak_active = max(self.peak_active, self.active)
            self.peak_per_domain[domain] = max(self.peak_per_domain[domain], self.active_per_domain[domain])
            try:
                yield
            finally:
                self.active_per_domain[domain] -= 1
                if not self.active_per_domain[domain]:
                    del self.active_per_domain[domain]

    @property
    def active(self) -> int:
        """Tasks currently holding a slot."""
        return self.active_per_domain.total()

    def get_stats(self) -> dict:
        return {
            "active": self.active,
            "active_per_domain": dict(self.active_per_domain),
            "peak_active": self.peak_active,
            "peak_per_domain": dict(self.peak_per_domain),
            "max_concurrent": self.max_concurrent,
            "max_per_domain": self.max_per_domain,
        }
