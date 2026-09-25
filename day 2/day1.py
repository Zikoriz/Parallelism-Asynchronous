"""Bridge to day 1 components.

Folder names contain a space ("day 1"), so they can't be imported as packages;
the day 1 folder is appended to sys.path instead and its modules re-exported here.
Day 2 must not define modules named like day 1 ones (client, config, main).
"""
import sys
from pathlib import Path

_DAY1 = Path(__file__).resolve().parent.parent / "day 1"
if str(_DAY1) not in sys.path:
    sys.path.append(str(_DAY1))

from client import AsyncCrawler, AsyncHTTPClient, FetchResult, Fetcher  # noqa: E402
from config import Config  # noqa: E402

__all__ = ["AsyncCrawler", "AsyncHTTPClient", "Config", "FetchResult", "Fetcher"]
