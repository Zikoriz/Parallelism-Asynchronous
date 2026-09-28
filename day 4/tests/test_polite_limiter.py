"""RateLimiter (per-domain / global spacing, min_delay, jitter, Crawl-delay, pause) on a virtual clock."""
import asyncio
import random

import pytest

from polite_limiter import RateLimiter, RateMonitor
from user_agents import UserAgentRotator


def grant_times(run_virtual, limiter: RateLimiter, domains: list[str | None]) -> dict[str | None, list[float]]:
    async def scenario():
        loop = asyncio.get_running_loop()
        times: dict[str | None, list[float]] = {}

        async def one(domain):
            await limiter.acquire(domain)
            times.setdefault(domain, []).append(loop.time())

        await asyncio.gather(*(one(d) for d in domains))
        return times

    return run_virtual(scenario())


def gaps(times: list[float]) -> list[float]:
    return [round(b - a, 9) for a, b in zip(times, times[1:])]


def test_single_domain_rate(run_virtual):
    times = grant_times(run_virtual, RateLimiter(requests_per_second=2.0), ["a.test"] * 5)
    assert times["a.test"][0] == 0.0
    assert gaps(times["a.test"]) == [0.5] * 4


def test_domains_have_independent_limits(run_virtual):
    times = grant_times(run_virtual, RateLimiter(requests_per_second=2.0), ["a.test", "b.test"] * 4)
    assert times["a.test"] == times["b.test"] == [0.0, 0.5, 1.0, 1.5]


def test_global_limit_when_not_per_domain(run_virtual):
    limiter = RateLimiter(requests_per_second=2.0, per_domain=False)
    times = grant_times(run_virtual, limiter, ["a.test", "b.test"] * 3)
    merged = sorted(times["a.test"] + times["b.test"])
    assert gaps(merged) == [0.5] * 5


def test_min_delay_wins_over_faster_rate(run_virtual):
    times = grant_times(run_virtual, RateLimiter(requests_per_second=10, min_delay=0.5), ["a.test"] * 4)
    assert gaps(times["a.test"]) == [0.5] * 3


def test_min_delay_alone(run_virtual):
    times = grant_times(run_virtual, RateLimiter(requests_per_second=None, min_delay=0.3), [None] * 3)
    assert gaps(times[None]) == [0.3, 0.3]


def test_unlimited(run_virtual):
    times = grant_times(run_virtual, RateLimiter(requests_per_second=None), ["a.test"] * 3)
    assert times["a.test"] == [0.0, 0.0, 0.0]


def test_jitter_adds_random_extra_delay(run_virtual):
    limiter = RateLimiter(requests_per_second=2.0, jitter=0.3, rng=random.Random(1))
    times = grant_times(run_virtual, limiter, ["a.test"] * 30)
    g = gaps(times["a.test"])
    assert all(0.5 <= x <= 0.8 for x in g)
    assert len(set(g)) > 20  # not constant


def test_crawl_delay_raises_interval_only_for_its_domain(run_virtual):
    limiter = RateLimiter(requests_per_second=2.0)
    limiter.set_crawl_delay("slow.test", 2.0)
    limiter.set_crawl_delay("fast.test", 0.1)  # lower than the rate: rate still applies
    times = grant_times(run_virtual, limiter, ["slow.test", "fast.test"] * 3)
    assert gaps(times["slow.test"]) == [2.0, 2.0]
    assert gaps(times["fast.test"]) == [0.5, 0.5]
    limiter.set_crawl_delay("slow.test", None)
    assert limiter.interval_for("slow.test") == 0.5


def test_pause_delays_next_request(run_virtual):
    async def scenario():
        limiter = RateLimiter(requests_per_second=None)
        loop = asyncio.get_running_loop()
        await limiter.acquire("a.test")
        limiter.pause("a.test", 3.0)
        await limiter.acquire("b.test")  # other domains are not paused
        b = loop.time()
        await limiter.acquire("a.test")
        return b, loop.time()

    assert run_virtual(scenario()) == (0.0, 3.0)


def test_acquire_returns_wait_and_monitor_stats(run_virtual):
    limiter = RateLimiter(requests_per_second=4.0)

    async def scenario():
        waits = [await limiter.acquire("a.test") for _ in range(5)]
        return waits, limiter.get_stats()

    waits, stats = run_virtual(scenario())
    assert waits == pytest.approx([0.0, 0.25, 0.25, 0.25, 0.25])
    assert stats["requests"] == 5
    assert stats["avg_interval"] == pytest.approx(0.25)
    assert stats["avg_wait"] == pytest.approx(0.2)
    assert stats["average_rps"] == pytest.approx(4.0)
    assert stats["current_rps"] == pytest.approx(4.0)
    assert stats["per_domain"]["a.test"]["requests"] == 5
    assert stats["configured_interval"] == 0.25


def test_monitor_current_rps_uses_window():
    m = RateMonitor(window=10.0)
    for t in (0.0, 1.0, 2.0):  # 1 req/s early on
        m.record("a", t, 0.0)
    for t in (50.0, 50.5, 51.0, 51.5):  # 2 req/s recently
        m.record("a", t, 0.0)
    assert m.current_rps(now=52.0) == pytest.approx(2.0)
    assert m.current_rps(now=100.0) == 0.0  # nothing in the last 10 s


@pytest.mark.parametrize("kwargs", [{"requests_per_second": -1}, {"min_delay": -1}, {"jitter": -0.1}])
def test_invalid_arguments(kwargs):
    with pytest.raises(ValueError):
        RateLimiter(**kwargs)


def test_user_agent_rotation():
    assert [UserAgentRotator("Solo/1").next() for _ in range(3)] == ["Solo/1"] * 3
    rr = UserAgentRotator(["A/1", "B/1", "C/1"])
    assert [rr.next() for _ in range(4)] == ["A/1", "B/1", "C/1", "A/1"]
    rnd = UserAgentRotator(["A/1", "B/1"], randomize=True, rng=random.Random(0))
    assert {rnd.next() for _ in range(20)} == {"A/1", "B/1"}
    with pytest.raises(ValueError):
        UserAgentRotator([])


def test_crawl_delay_learned_after_a_request_pushes_next_one(run_virtual):
    """robots.txt is fetched before its Crawl-delay is known; the next page must still wait."""
    async def scenario():
        limiter = RateLimiter(requests_per_second=10)
        loop = asyncio.get_running_loop()
        await limiter.acquire("a.test")  # robots.txt at t=0
        limiter.set_crawl_delay("a.test", 2.0)
        await limiter.acquire("a.test")
        return loop.time()

    assert run_virtual(scenario()) == 2.0


def test_stats_readable_outside_event_loop(run_virtual):
    limiter = RateLimiter(requests_per_second=2.0)
    run_virtual(limiter.acquire("a.test"))
    assert limiter.get_stats()["requests"] == 1
