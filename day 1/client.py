import asyncio
import time
from dataclasses import dataclass, field

import aiohttp

from config import Config


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

    async def __aenter__(self) -> "AsyncHTTPClient":
        connector = aiohttp.TCPConnector(
            limit=self.config.connector_limit,
            limit_per_host=self.config.connector_limit_per_host,
        )
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=self.config.total_timeout),
            headers={"User-Agent": self.config.user_agent},
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            await self._session.close()

    async def fetch(self, url: str) -> FetchResult:
        if self._session is None:
            raise RuntimeError("AsyncHTTPClient must be used as an async context manager")
        start = time.perf_counter()
        try:
            async with self._session.get(url) as resp:
                body = await resp.text(errors="replace")
                return FetchResult(
                    url=url,
                    success=True,
                    status=resp.status,
                    body=body,
                    headers=dict(resp.headers),
                    elapsed=time.perf_counter() - start,
                )
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            return FetchResult(
                url=url,
                success=False,
                elapsed=time.perf_counter() - start,
                error=f"{type(e).__name__}: {e}",
            )


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
