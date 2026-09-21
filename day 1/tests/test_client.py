import asyncio
import time

import aiohttp
from aioresponses import aioresponses

from client import AsyncHTTPClient, FetchResult, Fetcher
from config import Config


async def test_fetch_success():
    with aioresponses() as m:
        m.get("http://x.test/a", status=200, body="hello", headers={"X-Test": "1"})
        async with AsyncHTTPClient(Config()) as client:
            r = await client.fetch("http://x.test/a")
    assert r.success and r.status == 200
    assert r.body == "hello"
    assert r.headers["X-Test"] == "1"
    assert r.elapsed >= 0 and r.error is None


async def test_fetch_non_200_is_still_a_response():
    with aioresponses() as m:
        m.get("http://x.test/missing", status=404, body="nope")
        async with AsyncHTTPClient() as client:
            r = await client.fetch("http://x.test/missing")
    assert r.success and r.status == 404


async def test_client_error_returned_not_raised():
    with aioresponses() as m:
        m.get("http://x.test/boom", exception=aiohttp.ClientConnectionError("down"))
        async with AsyncHTTPClient() as client:
            r = await client.fetch("http://x.test/boom")
    assert not r.success and r.status is None
    assert "ClientConnectionError" in r.error


async def test_timeout_returned_not_raised():
    with aioresponses() as m:
        m.get("http://x.test/slow", exception=asyncio.TimeoutError())
        async with AsyncHTTPClient() as client:
            r = await client.fetch("http://x.test/slow")
    assert not r.success and "TimeoutError" in r.error


async def test_one_failure_does_not_break_batch():
    urls = ["http://x.test/ok1", "http://x.test/bad", "http://x.test/ok2"]
    with aioresponses() as m:
        m.get(urls[0], body="1")
        m.get(urls[1], exception=aiohttp.ClientError("bad"))
        m.get(urls[2], body="2")
        cfg = Config()
        async with AsyncHTTPClient(cfg) as client:
            results = await Fetcher(client, cfg).fetch_many(urls)
    assert [r.success for r in results] == [True, False, True]
    assert [r.url for r in results] == urls


async def test_session_closed_on_exit():
    client = AsyncHTTPClient()
    async with client:
        session = client._session
        assert not session.closed
    assert session.closed


class SlowClient:
    """Fake client that tracks how many fetches are in flight."""

    def __init__(self, delay: float):
        self.delay = delay
        self.in_flight = 0
        self.peak = 0

    async def fetch(self, url: str) -> FetchResult:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        await asyncio.sleep(self.delay)
        self.in_flight -= 1
        return FetchResult(url=url, success=True, status=200)


async def test_fetch_many_50_urls_parallel_and_bounded():
    cfg = Config(max_concurrency=10)
    client = SlowClient(delay=0.1)
    urls = [f"http://x.test/{i}" for i in range(50)]

    start = time.perf_counter()
    results = await Fetcher(client, cfg).fetch_many(urls)
    elapsed = time.perf_counter() - start

    assert len(results) == 50 and all(r.success for r in results)
    assert client.peak == 10  # never above max_concurrency, and actually reaches it
    assert elapsed < 50 * 0.1 / 2  # sequential would take 5s; ~0.5s expected
