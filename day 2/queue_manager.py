import asyncio
import logging
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger(__name__)

_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_url(url: str) -> str:
    """Dedup key for a URL.

    Lowercases scheme and host, drops default ports and the fragment, maps an empty
    path to "/" and strips a trailing slash from any other path, so
    "HTTP://Site.com:80/a/#x" and "http://site.com/a" are the same page.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if parts.port is not None and parts.port != _DEFAULT_PORTS.get(scheme):
        host = f"{host}:{parts.port}"
    path = parts.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    return urlunsplit((scheme, host, path, parts.query, ""))


def domain_allowed(url: str, allowed_domains: Iterable[str]) -> bool:
    """True if the URL's host is one of allowed_domains or a subdomain of one."""
    host = (urlsplit(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in allowed_domains)


class QueueManager:
    """Crawl frontier: asyncio.Queue of (url, depth) with dedup, depth and domain filters.

    A URL is accepted at most once over the whole crawl (dedup by normalize_url),
    so a page linked from many places or via a cycle is fetched only once.
    """

    def __init__(self, max_depth: int = 2, allowed_domains: Iterable[str] | None = None):
        self.max_depth = max_depth
        self.allowed_domains = {d.lower() for d in allowed_domains} if allowed_domains else None
        self._queue: asyncio.Queue[tuple[str, int]] = asyncio.Queue()
        self._seen: set[str] = set()

    def add(self, url: str, depth: int = 0) -> bool:
        """Enqueue url at depth; returns False if it was filtered out or already seen."""
        if depth > self.max_depth:
            return False
        if urlsplit(url).scheme.lower() not in _DEFAULT_PORTS:
            return False
        if self.allowed_domains is not None and not domain_allowed(url, self.allowed_domains):
            logger.debug("Skip %s: domain not allowed", url)
            return False
        key = normalize_url(url)
        if key in self._seen:
            return False
        self._seen.add(key)
        self._queue.put_nowait((url, depth))
        return True

    def add_many(self, urls: Iterable[str], depth: int) -> int:
        return sum(self.add(url, depth) for url in urls)

    async def get(self) -> tuple[str, int]:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()

    async def join(self) -> None:
        await self._queue.join()

    def empty(self) -> bool:
        return self._queue.empty()

    def qsize(self) -> int:
        return self._queue.qsize()

    @property
    def seen_count(self) -> int:
        return len(self._seen)

    def is_seen(self, url: str) -> bool:
        return normalize_url(url) in self._seen
