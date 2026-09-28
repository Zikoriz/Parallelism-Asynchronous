"""Day 4 AsyncCrawler end to end on a mock site, virtual clock."""
import logging

import pytest

from mock_server import MockServer, Reply, intervals, page
from polite_crawler import AsyncCrawler, PoliteSnapshot
from settings import Config

ROOT = "http://site.test/"
ROBOTS = "User-agent: *\nDisallow: /private/\nDisallow: /*.pdf$\n\nUser-agent: MyBot\nDisallow: /nobots/\n"
SITE = {
    "http://site.test/robots.txt": ROBOTS,
    ROOT: page("home", "/a", "/b", "/private/x", "/private/y", "/doc.pdf", "/nobots/z"),
    "http://site.test/a": page("a", "/private/x", "/c"),  # blocked link seen again
    "http://site.test/b": page("b"),
    "http://site.test/c": page("c"),
    "http://site.test/private/x": page("secret"),
    "http://site.test/private/y": page("secret"),
    "http://site.test/doc.pdf": page("pdf"),
    "http://site.test/nobots/z": page("z"),
}
FAST = Config(backoff_jitter=0.0, max_retries=2)


def run_crawl(run_virtual, routes, start=(ROOT,), crawl_kwargs=None, **kwargs):
    server = MockServer(routes)
    kwargs.setdefault("progress_interval", None)
    kwargs.setdefault("config", FAST)

    async def scenario():
        with server.running():
            async with AsyncCrawler(**kwargs) as crawler:
                results = await crawler.crawl(list(start), **(crawl_kwargs or {}))
                return crawler, results

    crawler, results = run_virtual(scenario())
    return crawler, results, server


def test_disallowed_urls_are_never_requested(run_virtual, caplog):
    caplog.set_level(logging.INFO, logger="polite_crawler")
    crawler, results, server = run_crawl(run_virtual, SITE, requests_per_second=None)
    requested = set(server.urls(path_only=True))
    assert requested == {"/robots.txt", "/", "/a", "/b", "/c", "/nobots/z"}
    assert set(crawler.blocked_urls) == {"http://site.test/private/x", "http://site.test/private/y",
                                         "http://site.test/doc.pdf"}
    assert all(v.startswith("RobotsDisallowedError") for v in crawler.blocked_urls.values())
    assert crawler.get_stats()["blocked"] == 3
    assert crawler.failed_urls == {}  # blocked links are not fetch failures
    assert server.urls(path_only=True).count("/robots.txt") == 1  # cached
    blocked_logs = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Blocked by robots.txt")]
    assert len(blocked_logs) == 3  # each blocked URL logged once, though /private/x is linked twice


def test_rules_for_specific_user_agent(run_virtual):
    crawler, _, server = run_crawl(run_virtual, SITE, requests_per_second=None, user_agent="MyBot/1.0")
    requested = set(server.urls(path_only=True))
    assert "/nobots/z" not in requested  # MyBot's own group forbids it
    assert "/private/x" in requested  # the "*" group doesn't apply to MyBot
    assert {r.user_agent for r in server.requests} == {"MyBot/1.0"}


def test_rotation_respects_robots_for_every_agent(run_virtual):
    crawler, _, server = run_crawl(run_virtual, SITE, requests_per_second=None,
                                   user_agents=["MyBot/1.0", "Other/2.0"])
    requested = set(server.urls(path_only=True))
    assert "/nobots/z" not in requested and "/private/x" not in requested
    assert {r.user_agent for r in server.requests} == {"MyBot/1.0", "Other/2.0"}


def test_blocked_start_url_is_skipped(run_virtual):
    crawler, results, server = run_crawl(run_virtual, SITE, start=["http://site.test/private/x"])
    assert results == [] and server.urls(path_only=True) == ["/robots.txt"]
    assert "http://site.test/private/x" in crawler.blocked_urls


def test_respect_robots_false(run_virtual):
    crawler, _, server = run_crawl(run_virtual, SITE, requests_per_second=None, respect_robots=False)
    requested = set(server.urls(path_only=True))
    assert "/robots.txt" not in requested and "/private/x" in requested
    assert crawler.blocked_urls == {}


def test_example_configuration_spacing(run_virtual):
    """Spec example: 2 req/s, min_delay 0.5 -> requests to one site 0.5 s apart."""
    crawler, results, server = run_crawl(
        run_virtual, SITE,
        max_concurrent=5, requests_per_second=2.0, respect_robots=True, min_delay=0.5, user_agent="MyBot/1.0",
    )
    times = server.times(host="site.test")  # robots.txt included: it is a request to the site too
    assert all(gap == pytest.approx(0.5) or gap > 0.5 for gap in intervals(times))
    assert intervals(times)[0] == pytest.approx(0.5)
    stats = crawler.get_stats()["rate"]
    assert stats["avg_interval"] >= 0.5 - 1e-9
    assert stats["average_rps"] <= 2.0 + 1e-9


def test_crawl_delay_from_robots_is_used(run_virtual):
    routes = {**SITE, "http://site.test/robots.txt": "User-agent: *\nCrawl-delay: 2\nDisallow: /private/"}
    crawler, _, server = run_crawl(run_virtual, routes, requests_per_second=10.0, max_concurrent=5)
    page_times = server.times(host="site.test")
    assert all(gap >= 2.0 - 1e-9 for gap in intervals(page_times))
    assert crawler.rate_limiter.crawl_delays == {"site.test": 2.0}


def test_different_domains_are_limited_independently(run_virtual):
    routes = {
        "http://a.test/": page("a", "/1", "/2", "/3"),
        **{f"http://a.test/{i}": page(str(i)) for i in (1, 2, 3)},
        "http://b.test/": page("b", "/1", "/2", "/3"),
        **{f"http://b.test/{i}": page(str(i)) for i in (1, 2, 3)},
    }  # no robots.txt on either: 404 -> everything allowed
    crawler, results, server = run_crawl(run_virtual, routes, start=["http://a.test/", "http://b.test/"],
                                         requests_per_second=1.0, max_concurrent=4)
    assert len(results) == 8
    for host in ("a.test", "b.test"):
        assert intervals(server.times(host=host)) == pytest.approx([1.0] * 4)  # robots + 4 pages
    # both sites in parallel: 5 requests each at 1 req/s take 4 s, not 9
    assert crawler.stats.elapsed == pytest.approx(4.0)


def test_global_limit_across_domains(run_virtual):
    routes = {"http://a.test/": page("a"), "http://b.test/": page("b")}
    _, _, server = run_crawl(run_virtual, routes, start=["http://a.test/", "http://b.test/"],
                             requests_per_second=2.0, per_domain=False, max_concurrent=4)
    assert intervals(sorted(server.times())) == pytest.approx([0.5] * 3)


def test_retries_inside_crawl_and_failures_do_not_stop_it(run_virtual):
    routes = {
        ROOT: page("home", "/flaky", "/broken", "/gone", "/ok"),
        "http://site.test/flaky": [Reply(500), Reply(500), Reply(body=page("flaky"))],
        "http://site.test/broken": Reply(503),
        "http://site.test/gone": Reply(404),
        "http://site.test/ok": page("ok"),
    }
    crawler, results, server = run_crawl(run_virtual, routes, requests_per_second=None)
    by_url = {r["url"]: r for r in results}
    assert by_url["http://site.test/flaky"]["error"] is None and by_url["http://site.test/flaky"]["attempts"] == 3
    assert by_url["http://site.test/ok"]["title"] == "ok"
    assert set(crawler.failed_urls) == {"http://site.test/broken", "http://site.test/gone"}
    assert "HTTP 503" in crawler.failed_urls["http://site.test/broken"]
    assert "attempts: 3" in crawler.failed_urls["http://site.test/broken"]
    assert "attempts: 1" in crawler.failed_urls["http://site.test/gone"]
    assert server.urls(path_only=True).count("/gone") == 1
    assert crawler.get_stats()["http"]["retries"] == 4  # 2 for /flaky, 2 for /broken


def test_progress_snapshot_has_delay_stats(run_virtual):
    snaps: list[PoliteSnapshot] = []
    crawler, _, _ = run_crawl(run_virtual, SITE, requests_per_second=2.0, progress_interval=1.0,
                              on_progress=snaps.append)
    final = snaps[-1]
    assert isinstance(final, PoliteSnapshot)
    assert final.blocked == 3 and final.processed == 5
    assert final.avg_interval == pytest.approx(0.5)
    text = final.format()
    assert "blocked=3" in text and "avg_interval=0.50s" in text and "rps=" in text


def test_fetch_urls_from_day1_goes_through_limits(run_virtual):
    server = MockServer({"http://site.test/1": "one", "http://site.test/2": [Reply(500), Reply(body="two")]})

    async def scenario():
        with server.running():
            async with AsyncCrawler(requests_per_second=1.0, config=FAST, progress_interval=None) as crawler:
                return await crawler.fetch_urls(["http://site.test/1", "http://site.test/2"])

    assert run_virtual(scenario()) == {"http://site.test/1": "one", "http://site.test/2": "two"}
    assert intervals(server.times()) == pytest.approx([1.0, 1.0])
