import pytest

from async_crawler import AsyncCrawler
from crawl_stats import ProgressSnapshot
from day2 import normalize_url
from mock_server import MockServer, page

ROOT = "http://site.test/"

# cycle (/ <-> /a), slash/fragment variants, an external link, a non-HTTP link,
# a 404 (/c), a chain deeper than depth 2, and pages for the pattern filters
SITE = {
    ROOT: page("home", "/a", "/b", "/a/", "/#top", "http://other.test/", "mailto:x@site.test", "/files/doc.pdf"),
    "http://site.test/a": page("a", "/", "/b", "/a/deep", "/a#frag"),
    "http://site.test/b": page("b", "/a/", "/c", "/catalogue/1"),
    "http://site.test/a/deep": page("deep", "/a/deep/deeper"),
    "http://site.test/a/deep/deeper": page("deeper"),
    "http://site.test/files/doc.pdf": page("pdf"),
    "http://site.test/catalogue/1": page("cat"),
    "http://other.test/": page("other", "http://other.test/x"),
    "http://other.test/x": page("other x"),
}


async def crawl(max_depth=2, max_concurrent=4, **kwargs):
    server = MockServer(SITE)
    with server.running():
        async with AsyncCrawler(max_concurrent=max_concurrent, max_depth=max_depth, progress_interval=None) as crawler:
            results = await crawler.crawl([ROOT], **kwargs)
    return crawler, results, server.urls()


async def test_depth_limit_and_depth_tracking():
    crawler, results, _ = await crawl(max_depth=2)
    depths = {r["url"]: r["depth"] for r in results}
    assert depths == {
        ROOT: 0,
        "http://site.test/a": 1, "http://site.test/b": 1, "http://site.test/files/doc.pdf": 1,
        "http://site.test/a/deep": 2, "http://site.test/c": 2, "http://site.test/catalogue/1": 2,
    }
    assert "http://site.test/a/deep/deeper" not in crawler.visited_urls  # depth 3


@pytest.mark.parametrize("max_depth, expected", [(0, 1), (1, 4)])
async def test_shallow_depths(max_depth, expected):
    _, results, calls = await crawl(max_depth=max_depth)
    assert len(results) == len(calls) == expected


@pytest.mark.parametrize("max_concurrent", [1, 8])
async def test_no_duplicates_in_visited_or_requests(max_concurrent):
    crawler, results, calls = await crawl(max_concurrent=max_concurrent)
    assert len(calls) == len(set(calls)) == len(results) == len(crawler.visited_urls)
    keys = {normalize_url(u) for u in crawler.visited_urls}
    assert len(keys) == len(crawler.visited_urls)  # "/a" and "/a/" are one page
    assert crawler.queue.get_stats()["duplicates"] > 0  # duplicates were seen and skipped


async def test_state_processed_and_failed():
    crawler, results, _ = await crawl()
    assert set(crawler.failed_urls) == {"http://site.test/c"}
    assert "404" in crawler.failed_urls["http://site.test/c"]
    assert set(crawler.processed_urls) == crawler.visited_urls - {"http://site.test/c"}
    assert crawler.processed_urls[ROOT]["title"] == "home"
    failed = next(r for r in results if r["url"] == "http://site.test/c")
    assert failed["status"] == 404 and failed["error"] and failed["links"] == []


async def test_same_domain_only():
    crawler, _, calls = await crawl(max_depth=2)
    assert not any("other.test" in u for u in calls)

    crawler, _, calls = await crawl(max_depth=2, same_domain_only=False)
    assert "http://other.test/" in crawler.visited_urls
    assert "http://other.test/x" in crawler.visited_urls
    assert not any(u.startswith("mailto:") for u in calls)


async def test_exclude_patterns():
    crawler, _, _ = await crawl(exclude_patterns=[r"\.pdf$", "/deep"])
    assert "http://site.test/files/doc.pdf" not in crawler.visited_urls
    assert "http://site.test/a/deep" not in crawler.visited_urls
    assert "http://site.test/a" in crawler.visited_urls


async def test_include_patterns_do_not_block_start_urls():
    crawler, _, _ = await crawl(include_patterns=["/catalogue/", r"/b$"])
    assert crawler.visited_urls == {ROOT, "http://site.test/b", "http://site.test/catalogue/1"}


@pytest.mark.parametrize("max_pages", [1, 3])
async def test_max_pages(max_pages):
    crawler, results, calls = await crawl(max_pages=max_pages, max_concurrent=8)
    assert len(results) == len(calls) == max_pages
    assert crawler.queue.closed


async def test_invalid_max_pages():
    async with AsyncCrawler(progress_interval=None) as crawler:
        with pytest.raises(ValueError):
            await crawler.crawl([ROOT], max_pages=0)


async def test_get_stats():
    crawler, _, _ = await crawl(max_concurrent=4)
    stats = crawler.get_stats()
    assert stats["processed"] == 6 and stats["failed"] == 1 and stats["queued"] == 0
    assert stats["in_progress"] == 0
    assert 1 <= stats["concurrency"]["peak_active"] <= 4
    assert stats["pages_per_sec"] > 0


def test_progress_reported_periodically(run_virtual):
    site = {ROOT: page("root", *(f"/p{i}" for i in range(9))), **{f"{ROOT}p{i}": page(str(i)) for i in range(9)}}
    server = MockServer(site)
    snaps: list[ProgressSnapshot] = []

    async def scenario():
        with server.running():
            async with AsyncCrawler(max_concurrent=4, max_depth=1, requests_per_second=2,
                                    progress_interval=1.0, on_progress=snaps.append) as crawler:
                return await crawler.crawl([ROOT])

    results = run_virtual(scenario())
    assert len(results) == 10
    # 10 requests at 2 rps = 4.5 s: reports at 1, 2, 3, 4 s + the final one
    assert [round(s.elapsed, 6) for s in snaps] == [1.0, 2.0, 3.0, 4.0, 4.5]
    # a request sent at the same instant as a report may or may not be counted yet
    assert [s.processed for s in snaps][:4] in ([2, 4, 6, 8], [3, 5, 7, 9])
    assert snaps[-1].processed == 10
    assert all(s.queued + s.in_progress + s.processed == 10 for s in snaps)
    assert snaps[0].in_progress == 4  # workers waiting for the rate limiter hold their URL
    assert snaps[-1].queued == snaps[-1].in_progress == snaps[-1].failed == 0
    assert snaps[-1].pages_per_sec == pytest.approx(10 / 4.5)
    assert snaps[-1].requests_per_sec == pytest.approx(2)
    assert "processed=10" in snaps[-1].format() and "pages/s" in snaps[-1].format()


async def test_example_from_spec():
    server = MockServer(SITE)
    with server.running():
        crawler = AsyncCrawler(max_concurrent=10, max_depth=2, progress_interval=None)
        results = await crawler.crawl(start_urls=[ROOT], max_pages=50, same_domain_only=True)
        await crawler.close()
    assert len(results) == 7
