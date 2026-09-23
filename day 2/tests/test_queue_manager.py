import pytest

from queue_manager import QueueManager, domain_allowed, normalize_url


@pytest.mark.parametrize(
    "a, b",
    [
        ("http://site.test/a/", "http://site.test/a"),
        ("http://site.test/a#frag", "http://site.test/a"),
        ("http://site.test/a/#frag", "http://site.test/a"),
        ("HTTP://Site.TEST/a", "http://site.test/a"),
        ("http://site.test:80/a", "http://site.test/a"),
        ("https://site.test:443/", "https://site.test"),
        ("http://site.test", "http://site.test/"),
    ],
)
def test_normalize_equivalent_urls(a, b):
    assert normalize_url(a) == normalize_url(b)


@pytest.mark.parametrize(
    "a, b",
    [
        ("http://site.test/a", "https://site.test/a"),
        ("http://site.test/a", "http://site.test/A"),
        ("http://site.test/a?x=1", "http://site.test/a?x=2"),
        ("http://site.test:8080/a", "http://site.test/a"),
    ],
)
def test_normalize_keeps_distinct_urls(a, b):
    assert normalize_url(a) != normalize_url(b)


def test_domain_allowed_includes_subdomains_only():
    allowed = {"site.test"}
    assert domain_allowed("http://site.test/x", allowed)
    assert domain_allowed("http://www.site.test/x", allowed)
    assert not domain_allowed("http://evilsite.test/x", allowed)
    assert not domain_allowed("http://other.test/x", allowed)


async def test_dedup_across_normalized_forms():
    q = QueueManager(max_depth=3)
    assert q.add("http://site.test/a", 0)
    assert not q.add("http://site.test/a/", 1)
    assert not q.add("http://site.test/a#x", 1)
    assert not q.add("HTTP://SITE.test/a", 2)
    assert q.qsize() == 1 and q.seen_count == 1
    assert q.is_seen("http://site.test/a/#y")


async def test_max_depth_and_scheme_filter():
    q = QueueManager(max_depth=1)
    assert q.add("http://site.test/", 0)
    assert q.add("http://site.test/1", 1)
    assert not q.add("http://site.test/2", 2)
    assert not q.add("mailto:me@site.test", 0)
    assert not q.add("ftp://site.test/f", 0)
    assert q.qsize() == 2
    # a URL rejected for depth is not remembered as seen
    assert not q.is_seen("http://site.test/2")


async def test_allowed_domains_filter():
    q = QueueManager(allowed_domains=["Site.test"])
    assert q.add("http://site.test/", 0)
    assert q.add("http://blog.site.test/", 0)
    assert not q.add("http://other.test/", 0)
    assert q.add_many(["http://site.test/a", "http://other.test/b", "http://site.test/a/"], 1) == 1


async def test_get_returns_fifo_with_depth():
    q = QueueManager()
    q.add_many(["http://site.test/1", "http://site.test/2"], 0)
    assert await q.get() == ("http://site.test/1", 0)
    q.task_done()
    assert await q.get() == ("http://site.test/2", 0)
    q.task_done()
    assert q.empty()
    await q.join()
