import asyncio

import pytest

from crawler_queue import CrawlerQueue


async def drain(q: CrawlerQueue) -> list[str]:
    out = []
    while (url := await q.get_next()) is not None:
        out.append(url)
        q.mark_processed(url)
    return out


async def test_higher_priority_first_fifo_within_priority():
    q = CrawlerQueue()
    q.add_url("http://s.test/low", priority=-1)
    q.add_url("http://s.test/a")
    q.add_url("http://s.test/high", priority=5)
    q.add_url("http://s.test/b")
    q.add_url("http://s.test/mid", priority=1)
    assert await drain(q) == [
        "http://s.test/high", "http://s.test/mid", "http://s.test/a", "http://s.test/b", "http://s.test/low",
    ]


async def test_duplicates_are_rejected_by_normalized_key():
    q = CrawlerQueue()
    assert q.add_url("http://s.test/a")
    assert not q.add_url("http://s.test/a/")
    assert not q.add_url("HTTP://S.TEST/a#frag")
    assert not q.add_url("http://s.test/a", priority=10)  # a higher priority doesn't re-add
    assert q.add_url("http://s.test/a?x=1")
    assert await drain(q) == ["http://s.test/a", "http://s.test/a?x=1"]
    assert not q.add_url("http://s.test/a")  # still rejected after processing
    assert q.get_stats()["duplicates"] == 4


async def test_depth_is_tracked_per_url():
    q = CrawlerQueue()
    q.add_url("http://s.test/", depth=0)
    q.add_url("http://s.test/x", depth=2)
    assert q.depth == {"http://s.test/": 0, "http://s.test/x": 2}


async def test_state_and_stats():
    q = CrawlerQueue()
    for u in ("http://s.test/1", "http://s.test/2", "http://s.test/3"):
        q.add_url(u)
    one, two = await q.get_next(), await q.get_next()
    assert q.get_stats() == {"queued": 1, "in_progress": 2, "visited": 2, "processed": 0,
                             "failed": 0, "added": 3, "duplicates": 0}
    q.mark_processed(one, {"title": "t"})
    q.mark_failed(two, "HTTP 500")
    assert q.processed == {one: {"title": "t"}}
    assert q.failed == {two: "HTTP 500"}
    assert q.visited == {one, two}
    assert q.get_stats()["in_progress"] == 0 and q.get_stats()["queued"] == 1


async def test_get_next_returns_none_when_empty_and_idle():
    assert await CrawlerQueue().get_next() is None


async def test_get_next_waits_while_work_is_in_progress():
    """An idle worker must not quit while another one may still add links."""
    q = CrawlerQueue()
    q.add_url("http://s.test/")
    first = await q.get_next()
    waiter = asyncio.create_task(q.get_next())
    await asyncio.sleep(0)
    assert not waiter.done()
    q.add_url("http://s.test/child")  # found on the page in progress
    q.mark_processed(first)
    assert await waiter == "http://s.test/child"


async def test_idle_waiters_released_when_last_url_finishes():
    q = CrawlerQueue()
    q.add_url("http://s.test/")
    url = await q.get_next()
    waiters = [asyncio.create_task(q.get_next()) for _ in range(3)]
    await asyncio.sleep(0)
    q.mark_failed(url, "boom")
    assert await asyncio.gather(*waiters) == [None, None, None]


async def test_close_stops_handing_out_urls():
    q = CrawlerQueue()
    q.add_url("http://s.test/1")
    q.add_url("http://s.test/2")
    q.close()
    assert await q.get_next() is None
    assert not q.add_url("http://s.test/3")


async def test_per_domain_cap_hands_out_other_domains():
    q = CrawlerQueue(max_per_domain=2)
    for i in range(4):
        q.add_url(f"http://a.test/{i}", priority=1)  # a.test URLs have higher priority
    q.add_url("http://b.test/0")
    got = [await q.get_next() for _ in range(3)]
    assert got == ["http://a.test/0", "http://a.test/1", "http://b.test/0"]  # a.test is at its cap

    waiter = asyncio.create_task(q.get_next())
    await asyncio.sleep(0)
    assert not waiter.done()  # only a.test URLs left, and a.test is busy
    q.mark_processed("http://a.test/0")
    assert await waiter == "http://a.test/2"


def test_invalid_cap():
    with pytest.raises(ValueError):
        CrawlerQueue(max_per_domain=0)
