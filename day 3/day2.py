"""Bridge to day 2 (and, through day 2's own bridge, day 1) components.

Folder names contain a space ("day 2"), so they can't be imported as packages;
the day 2 folder is appended to sys.path and its modules re-exported here.
Day 3 must not define modules named like day 1/2 ones
(client, config, crawler, day1, html_parser, queue_manager, main is per-day).
"""
import sys
from pathlib import Path

_DAY2 = Path(__file__).resolve().parent.parent / "day 2"
if str(_DAY2) not in sys.path:
    sys.path.append(str(_DAY2))

from crawler import AsyncCrawler  # noqa: E402
from day1 import AsyncHTTPClient, Config, FetchResult  # noqa: E402
from html_parser import DEFAULT_SELECTORS, HTMLParser  # noqa: E402
from queue_manager import normalize_url  # noqa: E402

__all__ = ["AsyncCrawler", "AsyncHTTPClient", "Config", "DEFAULT_SELECTORS", "FetchResult", "HTMLParser", "normalize_url"]
