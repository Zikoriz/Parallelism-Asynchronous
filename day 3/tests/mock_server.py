"""aioresponses-based mock HTTP server that records when each request arrived (loop.time())."""
import asyncio
import re
from contextlib import contextmanager
from http import HTTPStatus
from urllib.parse import urlsplit

from aioresponses import CallbackResult, aioresponses

from crawler_queue import normalize_url


def page(title: str, *hrefs: str) -> str:
    links = "".join(f'<a href="{h}">{h}</a>' for h in hrefs)
    return f"<html><head><title>{title}</title></head><body>{links}</body></html>"


class MockServer:
    def __init__(self, pages: dict[str, str], statuses: dict[str, int] | None = None, latency: float = 0.0):
        self.pages = {normalize_url(u): body for u, body in pages.items()}
        self.statuses = {normalize_url(u): s for u, s in (statuses or {}).items()}
        self.latency = latency
        self.requests: list[tuple[float, str]] = []  # (loop.time(), url) at arrival

    async def _handle(self, url, **kwargs) -> CallbackResult:
        self.requests.append((asyncio.get_running_loop().time(), str(url)))
        if self.latency:
            await asyncio.sleep(self.latency)
        key = normalize_url(str(url))
        status = self.statuses.get(key, 200 if key in self.pages else 404)
        if status != 200:
            # without reason aiohttp's raise_for_status() fails on an assert
            return CallbackResult(status=status, reason=HTTPStatus(status).phrase)
        return CallbackResult(body=self.pages[key], content_type="text/html")

    @contextmanager
    def running(self):
        with aioresponses() as m:
            m.get(re.compile(r".*"), callback=self._handle, repeat=True)
            yield self

    def urls(self) -> list[str]:
        return [u for _, u in self.requests]

    def times(self, host: str | None = None) -> list[float]:
        return [t for t, u in self.requests if host is None or urlsplit(u).hostname == host]


def intervals(times: list[float]) -> list[float]:
    return [b - a for a, b in zip(times, times[1:])]


def fan_out_site(root: str, n: int) -> dict[str, str]:
    """root links to n leaf pages; leaves have no links."""
    leaves = [f"{root}p{i}" for i in range(n)]
    return {root: page("root", *leaves), **{u: page(u) for u in leaves}}
