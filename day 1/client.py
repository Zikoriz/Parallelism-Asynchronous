import asyncio
import logging
import time
from dataclasses import dataclass, field, replace

import aiohttp

from config import Config

logger = logging.getLogger(__name__)


@dataclass
class FetchResult:
    url: str
    success: bool
    status: int | None = None
    body: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    elapsed: float = 0.0
    error: str | None = None


class AsyncHTTPClient:
    """Thin wrapper over a single shared aiohttp.ClientSession."""

    def __init__(self, config: Config | None = None):
        self.config = config or Config()
        self._session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        if self._session is not None and not self._session.closed:
            return
        connector = aiohttp.TCPConnector(
            limit=self.config.connector_limit,
            limit_per_host=self.config.connector_limit_per_host,
        )
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(
                total=self.config.total_timeout,
                connect=self.config.connect_timeout,
                sock_read=self.config.read_timeout,
            ),
            headers={"User-Agent": self.config.user_agent},
        )

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> "AsyncHTTPClient":
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def fetch(self, url: str) -> FetchResult:
        if self._session is None:
            raise RuntimeError("AsyncHTTPClient is not started: use it as an async context manager or call start()")
        logger.info("Fetching %s", url)
        start = time.perf_counter()
        try:
            async with self._session.get(url) as resp:
                resp.raise_for_status()
                body = await resp.text(errors="replace")
                result = FetchResult(
                    url=url,
                    success=True,
                    status=resp.status,
                    body=body,
                    headers=dict(resp.headers),
                    elapsed=time.perf_counter() - start,
                )
        except aiohttp.ClientResponseError as e:
            result = self._failure(url, start, e, status=e.status)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            result = self._failure(url, start, e)
        else:
            logger.info("Fetched %s: %d in %.0f ms", url, result.status, result.elapsed * 1000)
        return result

    @staticmethod
    def _failure(url: str, start: float, exc: Exception, status: int | None = None) -> FetchResult:
        elapsed = time.perf_counter() - start
        error = f"{type(exc).__name__}: {exc}"
        logger.warning("Failed %s after %.0f ms: %s", url, elapsed * 1000, error)
        return FetchResult(url=url, success=False, status=status, elapsed=elapsed, error=error)


class Fetcher:
    """Loads many URLs concurrently, bounded by config.max_concurrency."""

    def __init__(self, client: AsyncHTTPClient, config: Config | None = None):
        self.client = client
        self.config = config or client.config

    async def fetch_many(self, urls: list[str]) -> list[FetchResult]:
        semaphore = asyncio.Semaphore(self.config.max_concurrency)

        async def bounded(url: str) -> FetchResult:
            async with semaphore:
                return await self.client.fetch(url)

        return list(await asyncio.gather(*(bounded(u) for u in urls)))


class AsyncCrawler:
    """Public day-1 interface: {url: html} on top of AsyncHTTPClient + Fetcher.

    Failed pages are logged and mapped to an empty string, so one bad URL never
    breaks a batch.
    """

    def __init__(self, max_concurrent: int = 10, config: Config | None = None):
        self.config = replace(config or Config(), max_concurrency=max_concurrent)
        self._client = AsyncHTTPClient(self.config)
        self._fetcher = Fetcher(self._client, self.config)

    async def fetch_url(self, url: str) -> str:
        await self._client.start()
        result = await self._client.fetch(url)
        return result.body if result.success else ""

    async def fetch_urls(self, urls: list[str]) -> dict[str, str]:
        await self._client.start()
        results = await self._fetcher.fetch_many(urls)
        return {r.url: r.body if r.success else "" for r in results}

    async def close(self) -> None:
        await self._client.close()

    async def __aenter__(self) -> "AsyncCrawler":
        await self._client.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()
