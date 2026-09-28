import asyncio

import pytest

from semaphore_manager import SemaphoreManager


async def run_tasks(manager: SemaphoreManager, urls: list[str], hold: float = 0.01) -> list[dict]:
    snapshots = []

    async def task(url):
        async with manager.slot(url):
            snapshots.append(manager.get_stats())
            await asyncio.sleep(hold)

    await asyncio.gather(*(task(u) for u in urls))
    return snapshots


async def test_global_limit():
    manager = SemaphoreManager(max_concurrent=3)
    snaps = await run_tasks(manager, [f"http://h{i}.test/" for i in range(10)])
    assert max(s["active"] for s in snaps) == 3
    assert manager.peak_active == 3
    assert manager.active == 0 and manager.active_per_domain == {}


async def test_per_domain_limit_with_other_domains_in_parallel():
    manager = SemaphoreManager(max_concurrent=10, max_per_domain=2)
    urls = [f"http://a.test/{i}" for i in range(6)] + [f"http://b.test/{i}" for i in range(6)]
    snaps = await run_tasks(manager, urls)
    assert manager.peak_per_domain == {"a.test": 2, "b.test": 2}
    assert manager.peak_active == 4  # both domains at their cap simultaneously
    assert all(n <= 2 for s in snaps for n in s["active_per_domain"].values())


async def test_waiting_for_a_busy_domain_does_not_take_global_slots():
    """Requests queued behind a.test don't occupy global slots b.test could use."""
    manager = SemaphoreManager(max_concurrent=2, max_per_domain=1)
    order = []

    async def task(url, hold):
        async with manager.slot(url):
            order.append(url)
            await asyncio.sleep(hold)

    await asyncio.gather(
        task("http://a.test/1", 0.05), task("http://a.test/2", 0.05), task("http://a.test/3", 0.05),
        task("http://b.test/1", 0.0),
    )
    assert order.index("http://b.test/1") == 1  # right after the first a.test request


async def test_slot_released_on_exception():
    manager = SemaphoreManager(max_concurrent=1)
    with pytest.raises(RuntimeError):
        async with manager.slot("http://a.test/"):
            raise RuntimeError("boom")
    assert manager.active == 0
    async with asyncio.timeout(1):
        async with manager.slot("http://a.test/"):
            pass


def test_invalid_limits():
    with pytest.raises(ValueError):
        SemaphoreManager(max_concurrent=0)
    with pytest.raises(ValueError):
        SemaphoreManager(max_per_domain=0)
