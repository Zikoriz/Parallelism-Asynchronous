import asyncio
import logging
import random
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace

from day3 import AsyncCrawler as Day3AsyncCrawler
from day3 import HTMLParser, ProgressSnapshot, host_of, normalize_url
from exceptions import ParseError, RobotsDisallowedError
from polite_limiter import RateLimiter
from resilient_client import ResilientHTTPClient
from robots import RobotsParser, RobotsRules
from settings import Config
from user_agents import UserAgentRotator

logger = logging.getLogger(__name__)


@dataclass
class PoliteSnapshot(ProgressSnapshot):
    blocked: int = 0
    avg_interval: float | None = None  # mean gap between consecutive requests
    avg_wait: float = 0.0  # mean wait in the rate limiter
    retries: int = 0

    def format(self) -> str:
        interval = f"{self.avg_interval:.2f}s" if self.avg_interval is not None else "-"
        return (f"{super().format()} blocked={self.blocked} retries={self.retries} "
                f"avg_interval={interval} avg_wait={self.avg_wait:.2f}s")


class AsyncCrawler(Day3AsyncCrawler):
    """Day 3 crawler + politeness and resilience.

    - rate limiting (RateLimiter): requests_per_second, per_domain, min_delay, jitter;
      applied before every request, retries included;
    - robots.txt (RobotsParser, respect_robots=True): fetched once per site, Disallow
      checked for start URLs, for links before they are queued and again right before
      the request (sites first seen mid-crawl); Crawl-delay raises the domain's interval;
      blocked URLs are logged and collected in `blocked_urls`;
    - retries with exponential backoff + jitter, Retry-After on 429/503 (ResilientHTTPClient,
      Config.max_retries); a URL that still fails is marked failed and the crawl goes on;
    - User-Agent: `user_agent`, or rotation over `user_agents` (robots rules must allow
      the URL for every one of them).
    """

    def __init__(
        self,
        max_concurrent: int = 5,
        max_depth: int = 2,
        config: Config | None = None,
        parser: HTMLParser | None = None,
        *,
        requests_per_second: float | None = 1.0,
        per_domain: bool = True,
        min_delay: float = 0.0,
        jitter: float = 0.0,
        respect_robots: bool = True,
        user_agent: str | None = None,
        user_agents: Sequence[str] | None = None,
        rotate_randomly: bool = False,
        max_retries: int | None = None,
        max_per_domain: int | None = None,
        progress_interval: float | None = 1.0,
        on_progress: Callable[[ProgressSnapshot], None] | None = None,
        rng: random.Random | None = None,
    ):
        config = config or Config()
        if user_agent is not None:
            config = replace(config, user_agent=user_agent)
        if max_retries is not None:
            config = replace(config, max_retries=max_retries)
        super().__init__(max_concurrent, max_depth, config, parser, max_per_domain=max_per_domain,
                         progress_interval=progress_interval, on_progress=on_progress)
        self.requests_per_second = requests_per_second
        self.per_domain = per_domain
        self.min_delay = min_delay
        self.jitter = jitter
        self.respect_robots = respect_robots
        self.rng = rng or random.Random()
        self.user_agents = UserAgentRotator(user_agents or self.config.user_agent,
                                            randomize=rotate_randomly, rng=self.rng)
        self.rate_limiter = self._new_rate_limiter()
        self._client = ResilientHTTPClient(self.config, rate_limiter=self.rate_limiter,
                                           user_agents=self.user_agents, rng=self.rng)
        self._fetcher.client = self._client  # day 1 fetch_url/fetch_urls get retries and limits too
        self.robots = RobotsParser(self._client, self.user_agents.primary)
        self.blocked_urls: dict[str, str] = {}
        self._blocked_keys: set[str] = set()

    def _new_rate_limiter(self) -> RateLimiter:
        return RateLimiter(self.requests_per_second, self.per_domain,
                           min_delay=self.min_delay, jitter=self.jitter, rng=self.rng)

    def _make_rate_limiter(self) -> RateLimiter:
        return self.rate_limiter  # prepared in crawl(), before robots.txt of start URLs is fetched

    async def crawl(self, start_urls: Iterable[str], max_pages: int = 100, **kwargs) -> list[dict]:
        """Same as day 3 crawl() (same_domain_only, include/exclude_patterns); start URLs
        disallowed by robots.txt are skipped."""
        start_urls = list(start_urls)
        self.rate_limiter = self._new_rate_limiter()  # fresh schedule and statistics per crawl
        self._client.rate_limiter = self.rate_limiter
        self._client.counters.clear()
        self.blocked_urls, self._blocked_keys = {}, set()
        await self._client.start()
        if self.respect_robots:
            allowed = await asyncio.gather(*(self._robots_allows(u) for u in start_urls))
            start_urls = [u for u, ok in zip(start_urls, allowed) if ok]
        return await super().crawl(start_urls, max_pages, **kwargs)

    # -- robots.txt ------------------------------------------------------------

    async def _robots_allows(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        rules = await self.robots.get_rules(url)
        self._apply_crawl_delay(url, rules)
        return self._allowed_by_cached_robots(url)

    def _apply_crawl_delay(self, url: str, rules: RobotsRules) -> None:
        delays = [d for ua in self.user_agents.agents if (d := rules.crawl_delay(ua))]
        self.rate_limiter.set_crawl_delay(host_of(url), max(delays) if delays else None)

    def _allowed_by_cached_robots(self, url: str) -> bool:
        if self.robots.can_fetch_all(url, self.user_agents.agents):
            return True
        key = normalize_url(url)
        if key not in self._blocked_keys:
            self._blocked_keys.add(key)
            error = RobotsDisallowedError(url, self.user_agents.primary)
            self.blocked_urls[url] = f"{type(error).__name__}: {error}"
            logger.info("Blocked by robots.txt: %s", url)
        return False

    def _enqueue_links(self, links, depth, url_filter) -> int:
        if not self.respect_robots:
            return super()._enqueue_links(links, depth, url_filter)

        def allowed(link: str) -> bool:
            if not url_filter(link):
                return False
            # sites whose robots.txt isn't loaded yet are checked in _crawl_one
            return not self.robots.is_cached(link) or self._allowed_by_cached_robots(link)

        return super()._enqueue_links(links, depth, allowed)

    # -- one page --------------------------------------------------------------

    async def _crawl_one(self, url: str, depth: int) -> dict:
        if not await self._robots_allows(url):
            return await self._error_page(url, depth, self.blocked_urls.get(url) or "RobotsDisallowedError", attempts=0)
        fetched = await self._client.fetch(url, slot=self.semaphores.slot)
        try:
            page = await self._parse_result(fetched)
        except Exception as e:
            error = ParseError(url, e)
            logger.warning("Parse error for %s: %s", url, error)
            return await self._error_page(url, depth, f"{type(error).__name__}: {error}", fetched.attempts, fetched.status)
        page["depth"] = depth
        page["attempts"] = fetched.attempts
        return page

    async def _error_page(self, url: str, depth: int, error: str, attempts: int, status: int | None = None) -> dict:
        page = await self.parser.parse_html("", url)  # same keys, empty values
        page.update(depth=depth, status=status, error=error, attempts=attempts)
        return page

    # -- statistics ------------------------------------------------------------

    def snapshot(self) -> PoliteSnapshot:
        base = self.stats.snapshot(self.queue.get_stats(), self.rate_limiter.monitor.current_rps())
        rate = self.rate_limiter.monitor.get_stats()
        return PoliteSnapshot(**vars(base), blocked=len(self.blocked_urls), avg_interval=rate["avg_interval"],
                              avg_wait=rate["avg_wait"], retries=self._client.counters["retries"])

    def get_stats(self) -> dict:
        snap = self.snapshot()
        return {
            **self.queue.get_stats(),
            "elapsed": snap.elapsed,
            "pages_per_sec": snap.pages_per_sec,
            "blocked": len(self.blocked_urls),
            "rate": self.rate_limiter.get_stats(),
            "http": dict(self._client.counters),
            "robots_fetched": self.robots.fetch_count,
            "concurrency": self.semaphores.get_stats(),
        }

    def _log_rate_metrics(self) -> None:
        stats = self.rate_limiter.get_stats()
        fmt = lambda v: f"{v:.2f}" if v is not None else "-"  # noqa: E731
        logger.info("Requests: %d, average %.2f req/s, avg interval %ss, avg wait %.2fs, blocked by robots.txt: %d",
                    stats["requests"], stats["average_rps"], fmt(stats["avg_interval"]), stats["avg_wait"],
                    len(self.blocked_urls))
        for domain, d in stats["per_domain"].items():
            expected = self.rate_limiter.interval_for(domain)
            logger.info("  %s: %d requests, avg interval %ss (configured >= %.2fs)",
                        domain, d["requests"], fmt(d["avg_interval"]), expected)
