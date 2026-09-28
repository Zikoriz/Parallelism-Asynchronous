"""Bridge to day 3 (and, through its bridges, days 1-2) components.

Folder names contain a space ("day 3"), so they can't be imported as packages;
the day 3 folder is appended to sys.path and its modules re-exported here.
Day 4 must not define modules named like earlier ones (client, config, crawler,
html_parser, queue_manager, async_crawler, crawler_queue, rate_limiter,
semaphore_manager, url_filter, crawl_stats, day1, day2; main is per-day).
"""
import sys
from pathlib import Path

_DAY3 = Path(__file__).resolve().parent.parent / "day 3"
if str(_DAY3) not in sys.path:
    sys.path.append(str(_DAY3))

from async_crawler import AsyncCrawler  # noqa: E402
from crawl_stats import ProgressSnapshot  # noqa: E402
from day2 import AsyncHTTPClient, Config, FetchResult, HTMLParser, normalize_url  # noqa: E402
from semaphore_manager import SemaphoreManager  # noqa: E402
from url_filter import UrlFilter, host_of  # noqa: E402

__all__ = [
    "AsyncCrawler", "AsyncHTTPClient", "Config", "FetchResult", "HTMLParser", "ProgressSnapshot",
    "SemaphoreManager", "UrlFilter", "host_of", "normalize_url",
]
