import asyncio
import time

import logging

import aiohttp
import pytest
from aioresponses import aioresponses

from client import AsyncCrawler, AsyncHTTPClient, FetchResult, Fetcher
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


@pytest.mark.parametrize("status", [404, 500])
async def test_http_error_status_is_failure(status):
    with aioresponses() as m:
        m.get("http://x.test/err", status=status, body="nope")
        async with AsyncHTTPClient() as client:
            r = await client.fetch("http://x.test/err")
    assert not r.success and r.status == status
    assert "ClientResponseError" in r.error


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


async def test_session_uses_connect_and_read_timeouts():
    cfg = Config(total_timeout=30, connect_timeout=3, read_timeout=7)
    async with AsyncHTTPClient(cfg) as client:
        timeout = client._session.timeout
    assert (timeout.total, timeout.connect, timeout.sock_read) == (30, 3, 7)


async def test_fetch_logs_start_success_and_error(caplog):
    caplog.set_level(logging.INFO, logger="client")
    with aioresponses() as m:
        m.get("http://x.test/ok", body="ok")
        m.get("http://x.test/down", exception=aiohttp.ClientConnectionError("down"))
        async with AsyncHTTPClient() as client:
            await client.fetch("http://x.test/ok")
            await client.fetch("http://x.test/down")
    messages = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert (logging.INFO, "Fetching http://x.test/ok") in messages
    assert any(lvl == logging.INFO and msg.startswith("Fetched http://x.test/ok: 200") for lvl, msg in messages)
    assert any(
        lvl == logging.WARNING and "http://x.test/down" in msg and "ClientConnectionError" in msg
        for lvl, msg in messages
    )


async def test_crawler_fetch_url_returns_html():
    with aioresponses() as m:
        m.get("http://x.test/page", body="<html>hi</html>")
        crawler = AsyncCrawler(max_concurrent=5)
        try:
            html = await crawler.fetch_url("http://x.test/page")
        finally:
            await crawler.close()
    assert html == "<html>hi</html>"


async def test_crawler_fetch_urls_returns_dict_and_survives_errors():
    urls = ["http://x.test/1", "http://x.test/404", "http://x.test/down"]
    with aioresponses() as m:
        m.get(urls[0], body="one")
        m.get(urls[1], status=404)
        m.get(urls[2], exception=asyncio.TimeoutError())
        crawler = AsyncCrawler()
        results = await crawler.fetch_urls(urls)
        await crawler.close()
    assert results == {urls[0]: "one", urls[1]: "", urls[2]: ""}


async def test_crawler_respects_max_concurrent_and_closes():
    crawler = AsyncCrawler(max_concurrent=3)
    assert crawler.config.max_concurrency == 3
    slow = SlowClient(delay=0.05)
    crawler._fetcher.client = slow
    await crawler.fetch_urls([f"http://x.test/{i}" for i in range(10)])
    assert slow.peak == 3
    session = crawler._client._session
    await crawler.close()
    assert session.closed
