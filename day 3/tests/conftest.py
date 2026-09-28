import asyncio
import selectors

import pytest


class _VirtualTimeSelector(selectors.DefaultSelector):
    """Polls real sockets without blocking; when nothing is ready and the loop wants to
    sleep until its next timer, the virtual clock jumps there instead.

    While executor work (asyncio.to_thread, e.g. HTML parsing) is running it really
    waits for it instead: threads take zero virtual time, so they can't let the clock
    skip past timers that would have fired later.
    """

    loop: "VirtualTimeLoop"

    def select(self, timeout=None):
        if timeout is None or (timeout > 0 and self.loop.pending_threads):
            return super().select(None)  # woken by the self-pipe when a thread finishes
        events = super().select(0)
        if not events and timeout > 0:
            self.loop.now += timeout
        return events


class VirtualTimeLoop(asyncio.SelectorEventLoop):
    """Event loop whose time() is virtual: asyncio.sleep(0.2) completes instantly but
    loop.time() advances by exactly 0.2, so timing assertions are exact and fast."""

    def __init__(self) -> None:
        self.now = 0.0
        self.pending_threads = 0
        selector = _VirtualTimeSelector()
        selector.loop = self
        super().__init__(selector)

    def time(self) -> float:
        return self.now

    def run_in_executor(self, executor, func, *args):
        future = super().run_in_executor(executor, func, *args)
        self.pending_threads += 1
        future.add_done_callback(self._thread_done)
        return future

    def _thread_done(self, _future) -> None:
        self.pending_threads -= 1


@pytest.fixture
def run_virtual():
    """run_virtual(coro) -> result, executed on a fresh VirtualTimeLoop."""
    loops = []

    def run(coro):
        loop = VirtualTimeLoop()
        loops.append(loop)
        return loop.run_until_complete(coro)

    yield run
    for loop in loops:
        loop.run_until_complete(loop.shutdown_default_executor())
        loop.close()
