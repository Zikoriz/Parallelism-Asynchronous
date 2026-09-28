import asyncio
from dataclasses import dataclass


@dataclass
class ProgressSnapshot:
    elapsed: float
    processed: int
    failed: int
    queued: int
    in_progress: int
    pages_per_sec: float
    requests_per_sec: float | None  # measured request rate (rate limiter metric)

    def format(self) -> str:
        rps = f"{self.requests_per_sec:.2f}" if self.requests_per_sec is not None else "-"
        return (f"[{self.elapsed:6.1f}s] processed={self.processed} queued={self.queued} "
                f"in_progress={self.in_progress} errors={self.failed} "
                f"speed={self.pages_per_sec:.2f} pages/s rps={rps}")


class CrawlStats:
    """Timing for one crawl run; uses loop.time() so it works under a virtual clock."""

    def __init__(self) -> None:
        self.started_at: float | None = None
        self.finished_at: float | None = None

    def start(self) -> None:
        self.started_at = asyncio.get_running_loop().time()
        self.finished_at = None

    def finish(self) -> None:
        self.finished_at = asyncio.get_running_loop().time()

    @property
    def elapsed(self) -> float:
        if self.started_at is None:
            return 0.0
        end = self.finished_at if self.finished_at is not None else asyncio.get_running_loop().time()
        return end - self.started_at

    def snapshot(self, queue_stats: dict, requests_per_sec: float | None) -> ProgressSnapshot:
        elapsed = self.elapsed
        done = queue_stats["processed"] + queue_stats["failed"]
        return ProgressSnapshot(
            elapsed=elapsed,
            processed=queue_stats["processed"],
            failed=queue_stats["failed"],
            queued=queue_stats["queued"],
            in_progress=queue_stats["in_progress"],
            pages_per_sec=done / elapsed if elapsed > 0 else 0.0,
            requests_per_sec=requests_per_sec,
        )
