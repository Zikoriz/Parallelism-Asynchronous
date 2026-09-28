import asyncio
import heapq
import itertools
import logging
from collections import Counter
from typing import Any

from day2 import normalize_url
from url_filter import host_of

logger = logging.getLogger(__name__)


class CrawlerQueue:
    """Priority queue of URLs with dedup, processing state and an optional per-domain cap.

    - add_url(url, priority, depth): higher priority is served first, equal
      priorities in FIFO order. A URL (by normalize_url key) is accepted once per
      queue lifetime, so revisits and cycles are impossible.
    - get_next(): waits for a URL; returns None when the crawl is over: the queue is
      empty and no handed-out URL is still in progress (a worker in progress may
      still add links), or close() was called.
    - every URL returned by get_next() must be finished with mark_processed() or
      mark_failed().
    - max_per_domain: a domain with that many URLs in progress is skipped and the
      best URL of another domain is handed out instead, so workers don't all pile
      up behind one slow or rate-limited site (head-of-line blocking).

    State: visited (URLs handed out for processing), processed {url: result},
    failed {url: error}, depth {url: depth}.
    """

    def __init__(self, max_per_domain: int | None = None) -> None:
        if max_per_domain is not None and max_per_domain < 1:
            raise ValueError("max_per_domain must be >= 1")
        self.max_per_domain = max_per_domain
        self._heaps: dict[str, list[tuple[int, int, str]]] = {}  # domain -> (-priority, order, url)
        self._order = itertools.count()
        self._seen: set[str] = set()
        self._in_progress: set[str] = set()
        self._active: Counter[str] = Counter()  # domain -> URLs in progress
        self._changed = asyncio.Event()
        self._closed = False
        self.depth: dict[str, int] = {}
        self.visited: set[str] = set()
        self.processed: dict[str, Any] = {}
        self.failed: dict[str, str] = {}
        self.duplicates = 0

    def add_url(self, url: str, priority: int = 0, depth: int = 0) -> bool:
        """Enqueue url; False if it was already added before (or the queue is closed)."""
        if self._closed:
            return False
        key = normalize_url(url)
        if key in self._seen:
            self.duplicates += 1
            return False
        self._seen.add(key)
        self.depth[url] = depth
        heapq.heappush(self._heaps.setdefault(host_of(url), []), (-priority, next(self._order), url))
        self._changed.set()
        return True

    async def get_next(self) -> str | None:
        while True:
            if self._closed:
                return None
            domain = self._best_domain()
            if domain is not None:
                heap = self._heaps[domain]
                _, _, url = heapq.heappop(heap)
                if not heap:
                    del self._heaps[domain]
                self._in_progress.add(url)
                self._active[domain] += 1
                self.visited.add(url)
                return url
            if not self._in_progress:
                return None  # nothing queued and nobody can add more
            self._changed.clear()
            await self._changed.wait()  # new URL, or a domain got a free slot

    def _best_domain(self) -> str | None:
        """Domain whose head entry is best among domains below the per-domain cap."""
        best = None
        for domain, heap in self._heaps.items():
            if self.max_per_domain is not None and self._active[domain] >= self.max_per_domain:
                continue
            if best is None or heap[0] < self._heaps[best][0]:
                best = domain
        return best

    def mark_processed(self, url: str, result: Any = None) -> None:
        self.processed[url] = result
        self._finish(url)

    def mark_failed(self, url: str, error: str) -> None:
        self.failed[url] = error
        self._finish(url)

    def _finish(self, url: str) -> None:
        if url in self._in_progress:
            self._in_progress.discard(url)
            domain = host_of(url)
            self._active[domain] -= 1
            if not self._active[domain]:
                del self._active[domain]
        self._changed.set()  # waiters re-check: maybe the crawl is over now

    def close(self) -> None:
        """Stop handing out URLs: every current and future get_next() returns None."""
        self._closed = True
        self._changed.set()

    @property
    def closed(self) -> bool:
        return self._closed

    def qsize(self) -> int:
        return sum(len(h) for h in self._heaps.values())

    def is_seen(self, url: str) -> bool:
        return normalize_url(url) in self._seen

    def get_stats(self) -> dict:
        return {
            "queued": self.qsize(),
            "in_progress": len(self._in_progress),
            "visited": len(self.visited),
            "processed": len(self.processed),
            "failed": len(self.failed),
            "added": len(self._seen),
            "duplicates": self.duplicates,
        }
