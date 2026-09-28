"""Rate limiter timings. Async tests with run_virtual use a virtual clock: loop.time()
advances exactly by the slept amount, so ±10% checks run instantly and deterministically."""
import asyncio

import pytest

from async_crawler import AsyncCrawler
from mock_server import MockServer, fan_out_site, intervals
from rate_limiter import DomainRateLimiter, RateLimiter

TOLERANCE = 0.10


def assert_spacing(times: list[float], expected: float) -> None:
    gaps = intervals(times)
    assert gaps, "need at least two requests"
    for gap in gaps:
        assert gap == pytest.approx(expected, rel=TOLERANCE), gaps


async def grant_times(acquire, n: int) -> list[float]:
    loop = asyncio.get_running_loop()
    times = []

    async def one():
        await acquire()
        times.append(loop.time())

    await asyncio.gather(*(one() for _ in range(n)))
    return times


# --- RateLimiter -------------------------------------------------------------

def test_five_rps_spaces_grants_by_200ms(run_virtual):
    limiter = RateLimiter(requests_per_second=5)
    times = run_virtual(grant_times(limiter.acquire, 10))
    assert times[0] == 0.0  # first request is not delayed
    assert_spacing(times, 0.2)
    assert limiter.actual_rps() == pytest.approx(5, rel=TOLERANCE)


def test_unlimited_does_not_wait(run_virtual):
    limiter = RateLimiter(requests_per_second=None)
    assert run_virtual(grant_times(limiter.acquire, 5)) == [0.0] * 5


def test_burst_then_steady_rate(run_virtual):
    limiter = RateLimiter(requests_per_second=10, burst=3)
    times = run_virtual(grant_times(limiter.acquire, 6))
    assert times[:3] == [0.0, 0.0, 0.0]
    assert_spacing(times[2:], 0.1)


def test_idle_time_refills_only_up_to_burst(run_virtual):
    async def scenario():
        limiter = RateLimiter(requests_per_second=5)
        await limiter.acquire()
        await asyncio.sleep(5)  # long idle: still only one token saved
        return await grant_times(limiter.acquire, 3)

    times = run_virtual(scenario())
    assert times[0] == pytest.approx(5.0)
    assert_spacing(times, 0.2)


def test_context_manager_acquires(run_virtual):
    async def scenario():
        limiter = RateLimiter(requests_per_second=4)
        loop = asyncio.get_running_loop()
        times = []
        for _ in range(3):
            async with limiter:
                times.append(loop.time())
        return times

    assert_spacing(run_virtual(scenario()), 0.25)


@pytest.mark.parametrize("kwargs", [{"requests_per_second": -1}, {"requests_per_second": 5, "burst": 0}])
def test_invalid_arguments(kwargs):
    with pytest.raises(ValueError):
        RateLimiter(**kwargs)


async def test_real_clock_is_not_faster_than_limit():
    """Sanity check on the real event loop (0.2 s): grants are never early."""
    limiter = RateLimiter(requests_per_second=20)
    times = await grant_times(limiter.acquire, 5)
    assert times[-1] - times[0] >= 4 * 0.05 * (1 - TOLERANCE)


# --- DomainRateLimiter -------------------------------------------------------

def test_hosts_have_independent_buckets(run_virtual):
    limiter = DomainRateLimiter(per_host_rps=5)
    urls = [f"http://{h}.test/{i}" for i in range(5) for h in ("a", "b")]

    async def scenario():
        loop = asyncio.get_running_loop()
        times = {"a": [], "b": []}

        async def one(url):
            await limiter.acquire(url)
            times[url[7]].append(loop.time())

        await asyncio.gather(*(one(u) for u in urls))
        return times

    times = run_virtual(scenario())
    assert_spacing(times["a"], 0.2)
    assert_spacing(times["b"], 0.2)
    assert times["a"] == times["b"]  # a slow host doesn't delay the other one
    assert set(limiter.per_host_rate_limiter) == {"a.test", "b.test"}


def test_global_limit_applies_across_hosts(run_virtual):
    limiter = DomainRateLimiter(requests_per_second=5, per_host_rps=5)
    urls = [f"http://{h}.test/{i}" for i in range(4) for h in ("a", "b")]
    times = run_virtual(grant_times(lambda it=iter(urls): limiter.acquire(next(it)), len(urls)))
    assert_spacing(times, 0.2)
    assert limiter.check_limits() == []


def test_per_host_spacing_holds_when_global_limit_delays(run_virtual):
    """Host token is taken after the global one, so a global queue can't squeeze a host's gaps."""
    limiter = DomainRateLimiter(requests_per_second=4, per_host_rps=3)
    urls = [f"http://{h}.test/{i}" for i in range(6) for h in ("a", "b", "c")]

    async def scenario():
        loop = asyncio.get_running_loop()
        times: dict[str, list[float]] = {}

        async def one(url):
            await limiter.acquire(url)
            times.setdefault(url[7], []).append(loop.time())

        await asyncio.gather(*(one(u) for u in urls))
        return times

    for host_times in run_virtual(scenario()).values():
        assert min(intervals(host_times)) >= 1 / 3 * (1 - 1e-9)
    assert limiter.check_limits() == []


# --- load test through AsyncCrawler + mock server ----------------------------

ROOT = "http://site.test/"


@pytest.mark.parametrize("max_concurrent", [1, 5, 50])
def test_crawler_requests_at_five_rps(run_virtual, max_concurrent):
    server = MockServer(fan_out_site(ROOT, 20))

    async def scenario():
        with server.running():
            async with AsyncCrawler(max_concurrent=max_concurrent, max_depth=1,
                                    requests_per_second=5, progress_interval=None) as crawler:
                await crawler.crawl([ROOT], max_pages=100)
                return crawler

    crawler = run_virtual(scenario())
    assert len(server.requests) == 21
    assert_spacing(server.times(), 0.2)  # ≈200 ms ±10% between requests
    assert crawler.rate_limiter.global_limiter.actual_rps() == pytest.approx(5, rel=TOLERANCE)
    assert crawler.rate_limiter.check_limits() == []


@pytest.mark.parametrize("max_concurrent", [1, 10, 100])
def test_more_concurrency_never_exceeds_per_host_rps(run_virtual, max_concurrent):
    site = {**fan_out_site("http://a.test/", 15), **fan_out_site("http://b.test/", 15)}
    server = MockServer(site, latency=0.5)  # slow server: many requests overlap

    async def scenario():
        with server.running():
            async with AsyncCrawler(max_concurrent=max_concurrent, max_depth=1, max_per_domain=5,
                                    per_host_rps=5, progress_interval=None) as crawler:
                await crawler.crawl(["http://a.test/", "http://b.test/"], max_pages=100)
                return crawler

    crawler = run_virtual(scenario())
    assert len(server.requests) == 32
    for host in ("a.test", "b.test"):
        times = server.times(host)
        assert min(intervals(times)) >= 0.2 * (1 - TOLERANCE)
        assert crawler.rate_limiter.per_host_rate_limiter[host].actual_rps() <= 5 * (1 + TOLERANCE)
    assert crawler.rate_limiter.check_limits() == []
    if max_concurrent >= 10:
        # throughput is not degraded: both hosts are served in parallel at full rate,
        # so 32 requests with 0.5 s latency take ~16 intervals of 0.2 s + one latency
        assert crawler.stats.elapsed < 16 * 0.2 + 2 * 0.5 + 0.1
