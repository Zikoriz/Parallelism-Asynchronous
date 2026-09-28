"""ResilientHTTPClient retries. Tests use run_virtual: backoff sleeps advance a virtual
clock (loop.time()) instead of really sleeping, so delays are checked exactly."""
import asyncio
import random
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import aiohttp
import pytest

from exceptions import CrawlerError, FetchError, RateLimitExceeded
from mock_server import MockServer, Reply, intervals, page
from polite_limiter import RateLimiter
from resilient_client import ResilientHTTPClient
from retry import backoff_delay, is_retryable_status, parse_retry_after
from settings import Config
from user_agents import UserAgentRotator

URL = "http://site.test/page"
# no jitter: backoff is exactly 0.5, 1.0, 2.0, ...
EXACT = Config(max_retries=3, backoff_base=0.5, backoff_factor=2.0, backoff_jitter=0.0)


def fetch(run_virtual, routes, config=EXACT, url=URL, **client_kwargs):
    server = MockServer(routes)

    async def scenario():
        with server.running():
            async with ResilientHTTPClient(config, **client_kwargs) as client:
                return await client.fetch(url), client

    result, client = run_virtual(scenario())
    return result, server, client


# --- DoD ---------------------------------------------------------------------

def test_retry_500_twice_then_200_succeeds(run_virtual):
    result, server, client = fetch(run_virtual, {URL: [Reply(500), Reply(500), Reply(body=page("ok"))]})
    assert result.success and result.status == 200 and "ok" in result.body
    assert result.attempts == 3 and result.exception is None
    assert server.times() == [0.0, 0.5, 1.5]  # backoff 0.5 s, then 1.0 s
    assert client.counters == {"attempts": 3, "retries": 2, "succeeded": 1}


def test_retry_not_done_for_permanent_404(run_virtual):
    result, server, client = fetch(run_virtual, {URL: Reply(404)})
    assert not result.success and result.status == 404
    assert len(server.requests) == 1  # no retries at all
    assert isinstance(result.exception, FetchError) and not result.exception.retryable
    assert "HTTP 404" in result.error and "attempts: 1" in result.error


# --- which errors are retried ------------------------------------------------

@pytest.mark.parametrize("status", [400, 401, 403, 404, 405, 410, 451])
def test_retry_skipped_for_client_errors(run_virtual, status):
    result, server, _ = fetch(run_virtual, {URL: Reply(status)})
    assert len(server.requests) == 1 and result.status == status


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_retry_exhausted_on_server_errors(run_virtual, status):
    result, server, client = fetch(run_virtual, {URL: Reply(status)})
    assert not result.success and result.status == status
    assert len(server.requests) == 1 + EXACT.max_retries
    assert server.times() == [0.0, 0.5, 1.5, 3.5]
    assert "attempts: 4" in result.error
    assert client.counters["failed"] == 1 and client.counters["retries"] == 3


@pytest.mark.parametrize("exc", [
    aiohttp.ClientConnectionError("connection refused"),
    aiohttp.ServerDisconnectedError(),
    asyncio.TimeoutError(),
])
def test_retry_on_connection_errors_and_timeouts(run_virtual, exc):
    result, server, _ = fetch(run_virtual, {URL: [Reply(raises=exc), Reply(body="ok")]})
    assert result.success and result.attempts == 2
    assert server.times() == [0.0, 0.5]


def test_retry_gives_up_on_non_retryable_client_error(run_virtual):
    result, server, _ = fetch(run_virtual, {URL: Reply(raises=aiohttp.InvalidURL(URL))})
    assert not result.success and len(server.requests) == 1
    assert "InvalidURL" in result.error


def test_retry_zero_retries(run_virtual):
    result, server, _ = fetch(run_virtual, {URL: Reply(500)}, config=Config(max_retries=0))
    assert not result.success and len(server.requests) == 1


# --- 429 / Retry-After -------------------------------------------------------

def test_retry_429_honours_retry_after_seconds(run_virtual):
    result, server, _ = fetch(run_virtual, {URL: [Reply(429, headers={"Retry-After": "3"}), Reply(body="ok")]})
    assert result.success and server.times() == [0.0, 3.0]


def test_retry_429_without_retry_after_uses_backoff(run_virtual):
    result, server, _ = fetch(run_virtual, {URL: [Reply(429), Reply(429), Reply(body="ok")]})
    assert result.success and server.times() == [0.0, 0.5, 1.5]


def test_retry_429_exhausted_reports_rate_limit(run_virtual):
    result, _, _ = fetch(run_virtual, {URL: Reply(429, headers={"Retry-After": "1"})})
    assert isinstance(result.exception, RateLimitExceeded) and result.status == 429
    assert result.error.startswith("RateLimitExceeded: HTTP 429")


def test_retry_503_retry_after_and_cap(run_virtual):
    config = Config(max_retries=1, backoff_jitter=0.0, max_retry_after=10)
    result, server, _ = fetch(run_virtual, {URL: [Reply(503, headers={"Retry-After": "3600"}), Reply(body="ok")]},
                              config=config)
    assert result.success and server.times() == [0.0, 10.0]  # an hour is capped at max_retry_after


def test_retry_429_pauses_whole_domain_in_rate_limiter(run_virtual):
    """Other requests to the throttled site wait for Retry-After too; other sites don't."""
    server = MockServer({
        URL: [Reply(429, headers={"Retry-After": "5"}), Reply(body="ok")],
        "http://site.test/other": Reply(body="ok", latency=0.0),
        "http://else.test/": Reply(body="ok"),
    })

    async def scenario():
        limiter = RateLimiter(requests_per_second=None)
        with server.running():
            async with ResilientHTTPClient(EXACT, rate_limiter=limiter) as client:
                first = asyncio.create_task(client.fetch(URL))
                await asyncio.sleep(0.1)  # the 429 has arrived
                await asyncio.gather(client.fetch("http://site.test/other"), client.fetch("http://else.test/"))
                await first

    run_virtual(scenario())
    assert server.times("http://site.test/other") == [pytest.approx(5.0)]
    assert server.times("http://else.test/") == [pytest.approx(0.1)]
    assert server.times(URL) == [0.0, pytest.approx(5.0)]


# --- limits, timeouts, User-Agent --------------------------------------------

def test_retry_attempts_go_through_rate_limiter(run_virtual):
    limiter = RateLimiter(requests_per_second=1)  # 1 s between requests beats 0.5 s backoff
    result, server, _ = fetch(run_virtual, {URL: [Reply(500), Reply(500), Reply(body="ok")]}, rate_limiter=limiter)
    assert result.success
    assert all(gap >= 1.0 - 1e-9 for gap in intervals(server.times()))


def test_retry_stops_at_operation_timeout(run_virtual):
    config = Config(max_retries=10, backoff_base=1.0, backoff_jitter=0.0, operation_timeout=5.0)
    result, server, _ = fetch(run_virtual, {URL: Reply(500)}, config=config)
    assert not result.success and result.status == 500
    assert "operation timeout 5s exceeded" in result.error and "HTTP 500" in result.error
    assert server.times() == [0.0, 1.0, 3.0]  # the next retry (at 7 s) is past the deadline


def test_retry_delays_are_not_real_sleeps(run_virtual):
    config = Config(max_retries=3, backoff_base=10.0, backoff_max=100.0, backoff_jitter=0.0)
    started = time.perf_counter()
    result, server, _ = fetch(run_virtual, {URL: [Reply(500)] * 3 + [Reply(body="ok")]}, config=config)
    assert result.success and server.times() == [0.0, 10.0, 30.0, 70.0]  # 70 s of backoff...
    assert time.perf_counter() - started < 5  # ...in well under a second of real time


def test_retry_logs_url_attempt_and_reason(run_virtual, caplog):
    caplog.set_level("INFO", logger="resilient_client")
    fetch(run_virtual, {URL: [Reply(502), Reply(body="ok")]})
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith(f"Retry 1/3 for {URL} in 0.50s") and "HTTP 502" in m for m in messages)
    assert f"Fetched {URL} on attempt 2" in messages


def test_retry_user_agent_rotation_per_attempt(run_virtual):
    agents = UserAgentRotator(["BotA/1.0", "BotB/2.0"])
    _, server, _ = fetch(run_virtual, {URL: [Reply(500), Reply(500), Reply(body="ok")]}, user_agents=agents)
    assert [r.user_agent for r in server.requests] == ["BotA/1.0", "BotB/2.0", "BotA/1.0"]


def test_retry_default_user_agent_from_config(run_virtual):
    _, server, _ = fetch(run_virtual, {URL: "ok"}, config=Config(user_agent="MyBot/1.0"))
    assert server.requests[0].user_agent == "MyBot/1.0"


# --- pure helpers ------------------------------------------------------------

def test_retry_backoff_is_exponential_and_capped():
    config = Config(backoff_base=0.5, backoff_factor=2.0, backoff_max=5.0, backoff_jitter=0.0)
    assert [backoff_delay(n, config) for n in range(1, 7)] == [0.5, 1.0, 2.0, 4.0, 5.0, 5.0]


def test_retry_backoff_jitter_bounds():
    config = Config(backoff_base=1.0, backoff_factor=2.0, backoff_jitter=0.5)
    rng = random.Random(42)
    delays = [backoff_delay(3, config, rng) for _ in range(200)]
    assert all(4.0 <= d <= 6.0 for d in delays)
    assert len(set(delays)) > 100  # really random, not a constant


@pytest.mark.parametrize("status, expected", [(429, True), (500, True), (503, True), (599, True),
                                              (400, False), (404, False), (499, False)])
def test_retry_statuses(status, expected):
    assert is_retryable_status(status) is expected


def test_retry_after_parsing():
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    assert parse_retry_after("120") == 120.0
    assert parse_retry_after(format_datetime(now + timedelta(seconds=30), usegmt=True), now) == 30.0
    assert parse_retry_after(format_datetime(now - timedelta(seconds=30), usegmt=True), now) == 0.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("soon") is None
    assert parse_retry_after("-5") is None


def test_exception_hierarchy():
    assert issubclass(RateLimitExceeded, FetchError) and issubclass(FetchError, CrawlerError)
    e = RateLimitExceeded("http://x/", retry_after=2)
    assert e.url == "http://x/" and e.status == 429 and e.retryable and e.retry_after == 2
