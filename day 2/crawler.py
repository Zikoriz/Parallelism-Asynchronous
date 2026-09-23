import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from day1 import AsyncHTTPClient, FetchResult
from html_parser import DEFAULT_SELECTORS, HTMLParser
from queue_manager import QueueManager

logger = logging.getLogger(__name__)


@dataclass
class PageResult:
    url: str
    depth: int
    success: bool
    status: int | None = None
    data: dict = field(default_factory=dict)
    links: list[str] = field(default_factory=list)
    error: str | None = None


class Crawler:
    """Take URL from queue -> fetch (day 1 client) -> parse -> enqueue new links.

    Bounded by max_depth (seeds are depth 0) and max_pages (total fetches).
    Worker count = number of pages fetched at the same time.
    """

    def __init__(
        self,
        client: AsyncHTTPClient,
        parser: HTMLParser | None = None,
        *,
        max_depth: int = 2,
        max_pages: int = 100,
        workers: int | None = None,
        allowed_domains: Iterable[str] | None = None,
        selectors: dict | None = None,
    ):
        self.client = client
        self.parser = parser or HTMLParser()
        self.max_depth = max_depth
        self.max_pages = max_pages
        self.workers = workers or client.config.max_concurrency
        self.allowed_domains = allowed_domains
        self.selectors = selectors if selectors is not None else DEFAULT_SELECTORS

    async def crawl(self, seed_urls: Iterable[str]) -> list[PageResult]:
        """Crawl from seed_urls; by default stays on the seeds' domains."""
        seed_urls = list(seed_urls)
        allowed = self.allowed_domains
        if allowed is None:
            allowed = {urlsplit(u).hostname for u in seed_urls if urlsplit(u).hostname}
        queue = QueueManager(max_depth=self.max_depth, allowed_domains=allowed)
        queue.add_many(seed_urls, depth=0)

        results: list[PageResult] = []
        started = 0  # pages taken for fetching; checked and bumped without awaits in between

        async def worker() -> None:
            nonlocal started
            while True:
                url, depth = await queue.get()
                try:
                    if started >= self.max_pages:
                        continue  # limit reached: drain the rest of the queue
                    started += 1
                    page = await self._process(url, depth)
                    results.append(page)
                    if page.success:
                        added = queue.add_many(page.links, depth + 1)
                        logger.info("Crawled %s (depth %d): %d links, %d new", url, depth, len(page.links), added)
                except Exception:
                    logger.exception("Unexpected error while processing %s", url)
                finally:
                    queue.task_done()

        tasks = [asyncio.create_task(worker()) for _ in range(self.workers)]
        try:
            await queue.join()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("Crawl finished: %d pages, %d unique URLs seen", len(results), queue.seen_count)
        return results

    async def _process(self, url: str, depth: int) -> PageResult:
        fetched: FetchResult = await self.client.fetch(url)
        if not fetched.success:
            return PageResult(url=url, depth=depth, success=False, status=fetched.status, error=fetched.error)
        page = PageResult(url=url, depth=depth, success=True, status=fetched.status)
        if _is_html(fetched.headers):
            page.data = self.parser.extract_data(fetched.body, self.selectors)
            page.links = self.parser.extract_links(fetched.body, url)
        return page


def _is_html(headers: dict[str, str]) -> bool:
    content_type = next((v for k, v in headers.items() if k.lower() == "content-type"), "")
    return not content_type or "html" in content_type.lower()
