import asyncio
import logging
from collections.abc import Callable, Iterable

from crawl_stats import CrawlStats, ProgressSnapshot
from crawler_queue import CrawlerQueue
from day2 import AsyncCrawler as Day2AsyncCrawler
from day2 import Config, HTMLParser
from rate_limiter import DomainRateLimiter
from semaphore_manager import SemaphoreManager
from url_filter import Pattern, UrlFilter, host_of

logger = logging.getLogger(__name__)


class AsyncCrawler(Day2AsyncCrawler):
    """Day 2 AsyncCrawler + crawl(): queue, depth limit, URL filters, concurrency and rate limits.

    - max_concurrent: worker count and global concurrency limit (SemaphoreManager);
      max_per_domain: simultaneous requests to one host (default: no extra limit).
      Set it below max_concurrent when crawling several sites with a per-host rate:
      the queue then hands workers other domains' URLs instead of letting them all
      wait behind one rate-limited host.
    - requests_per_second: global rate limit; per_host_rps: limit for each host
      (DomainRateLimiter.per_host_rate_limiter). None = unlimited.
    - max_depth: start URLs are depth 0, links found on a depth-d page are d+1;
      links deeper than max_depth are not enqueued.
    - progress: every progress_interval seconds a ProgressSnapshot goes to
      on_progress (default: logged at INFO); a final one is sent when crawl() ends.

    State of the last crawl: visited_urls (set of fetched URLs), processed_urls
    {url: page dict}, failed_urls {url: error}.
    """

    def __init__(
        self,
        max_concurrent: int = 10,
        max_depth: int = 2,
        config: Config | None = None,
        parser: HTMLParser | None = None,
        *,
        max_per_domain: int | None = None,
        requests_per_second: float | None = None,
        per_host_rps: float | None = None,
        progress_interval: float | None = 1.0,
        on_progress: Callable[[ProgressSnapshot], None] | None = None,
    ):
        super().__init__(max_concurrent, config, parser)
        self.max_concurrent = max_concurrent
        self.max_depth = max_depth
        self.max_per_domain = max_per_domain
        self.requests_per_second = requests_per_second
        self.per_host_rps = per_host_rps
        self.progress_interval = progress_interval
        self.on_progress = on_progress or (lambda snap: logger.info("%s", snap.format()))

        self.visited_urls: set[str] = set()
        self.processed_urls: dict[str, dict] = {}
        self.failed_urls: dict[str, str] = {}
        self.queue: CrawlerQueue | None = None
        self.semaphores: SemaphoreManager | None = None
        self.rate_limiter: DomainRateLimiter | None = None
        self.stats = CrawlStats()

    async def crawl(
        self,
        start_urls: Iterable[str],
        max_pages: int = 100,
        *,
        same_domain_only: bool = True,
        include_patterns: Iterable[Pattern] | None = None,
        exclude_patterns: Iterable[Pattern] | None = None,
    ) -> list[dict]:
        """Crawl from start_urls; returns a page dict (HTMLParser.parse_html + depth, status, error)
        for every fetched URL, failed ones included (error is set)."""
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        start_urls = list(start_urls)
        url_filter = UrlFilter(
            {host_of(u) for u in start_urls},
            same_domain_only=same_domain_only,
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
        )
        self.queue = queue = CrawlerQueue(self.max_per_domain)
        self.visited_urls, self.processed_urls, self.failed_urls = queue.visited, queue.processed, queue.failed
        self.semaphores = SemaphoreManager(self.max_concurrent, self.max_per_domain)
        self.rate_limiter = self._make_rate_limiter()
        for url in start_urls:
            queue.add_url(url, priority=0, depth=0)  # start URLs bypass the filters

        results: list[dict] = []
        started = 0  # URLs taken for fetching; checked and bumped with no await in between

        async def worker() -> None:
            nonlocal started
            while (url := await queue.get_next()) is not None:
                started += 1
                if started >= max_pages:
                    queue.close()  # this URL is the last one; in-progress ones still finish
                depth = queue.depth[url]
                try:
                    page = await self._crawl_one(url, depth)
                except Exception as e:  # never let one page kill a worker
                    logger.exception("Unexpected error while processing %s", url)
                    page = await self.parser.parse_html("", url)
                    page.update(depth=depth, status=None, error=f"{type(e).__name__}: {e}")
                results.append(page)
                if page["error"] is None and depth < self.max_depth:
                    self._enqueue_links(page["links"], depth + 1, url_filter)
                # mark only after new links are queued: otherwise idle workers could see
                # "empty queue, nothing in progress" and stop too early
                if page["error"] is None:
                    queue.mark_processed(url, page)
                else:
                    queue.mark_failed(url, page["error"])

        await self._client.start()
        self.stats.start()
        reporter = asyncio.create_task(self._report_progress()) if self.progress_interval else None
        workers = [asyncio.create_task(worker()) for _ in range(self.max_concurrent)]
        try:
            await asyncio.gather(*workers)
        finally:
            for t in workers:
                t.cancel()
            if reporter is not None:
                reporter.cancel()
            await asyncio.gather(*workers, *([reporter] if reporter else []), return_exceptions=True)
            self.stats.finish()

        self.on_progress(self.snapshot())
        self._log_rate_metrics()
        return results

    def _make_rate_limiter(self) -> DomainRateLimiter:
        """Fresh limiter (and RPS metrics) for each crawl; later days override it."""
        return DomainRateLimiter(self.requests_per_second, self.per_host_rps)

    async def _crawl_one(self, url: str, depth: int) -> dict:
        async with self.semaphores.slot(url):
            await self.rate_limiter.acquire(url)
            fetched = await self._client.fetch(url)
        page = await self._parse_result(fetched)  # parsing doesn't hold a network slot
        page["depth"] = depth
        return page

    def _enqueue_links(self, links: list[str], depth: int, url_filter: UrlFilter) -> int:
        # shallower pages get higher priority: the crawl goes breadth-first
        added = sum(self.queue.add_url(link, priority=-depth, depth=depth) for link in links if url_filter(link))
        logger.debug("Depth %d: %d links, %d new", depth, len(links), added)
        return added

    def snapshot(self) -> ProgressSnapshot:
        return self.stats.snapshot(self.queue.get_stats(), self.rate_limiter.global_limiter.actual_rps())

    def get_stats(self) -> dict:
        """Queue counters, concurrency peaks, measured request rates and timing of the last crawl."""
        snap = self.snapshot()
        return {
            **self.queue.get_stats(),
            "elapsed": snap.elapsed,
            "pages_per_sec": snap.pages_per_sec,
            "requests_per_sec": self.rate_limiter.actual_rps(),
            "concurrency": self.semaphores.get_stats(),
        }

    async def _report_progress(self) -> None:
        while True:
            await asyncio.sleep(self.progress_interval)
            self.on_progress(self.snapshot())

    def _log_rate_metrics(self) -> None:
        rates = self.rate_limiter.actual_rps()
        fmt = lambda r: f"{r:.2f}" if r is not None else "-"  # noqa: E731
        logger.info("Actual RPS: global=%s (limit %s); per host: %s",
                    fmt(rates["global"]), self.requests_per_second,
                    ", ".join(f"{h}={fmt(r)}" for h, r in rates["per_host"].items()) or "-")
        for problem in self.rate_limiter.check_limits():
            logger.warning("Rate limit exceeded by more than 10%%: %s", problem)
