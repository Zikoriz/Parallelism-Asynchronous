"""aioresponses-based scripted mock server.

Each URL maps to a Reply or a list of Replies consumed one per request (the last
one repeats). A Reply can also raise an exception (connection error, timeout).
Every request is recorded with its loop.time() and User-Agent.
"""
import asyncio
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from http import HTTPStatus
from urllib.parse import urlsplit

from aioresponses import CallbackResult, aioresponses

from day3 import normalize_url


@dataclass
class Reply:
    status: int = 200
    body: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    content_type: str = "text/html"
    raises: BaseException | None = None
    latency: float = 0.0


def page(title: str, *hrefs: str) -> str:
    links = "".join(f'<a href="{h}">{h}</a>' for h in hrefs)
    return f"<html><head><title>{title}</title></head><body>{links}</body></html>"


@dataclass
class Request:
    at: float
    url: str
    user_agent: str | None


class MockServer:
    def __init__(self, routes: dict[str, Reply | list[Reply] | str]):
        self.routes: dict[str, list[Reply]] = {}
        for url, reply in routes.items():
            if isinstance(reply, str):
                reply = Reply(body=reply)
            self.routes[normalize_url(url)] = list(reply) if isinstance(reply, list) else [reply]
        self.requests: list[Request] = []

    async def _handle(self, url, **kwargs) -> CallbackResult:
        headers = kwargs.get("headers") or {}
        self.requests.append(Request(asyncio.get_running_loop().time(), str(url), headers.get("User-Agent")))
        replies = self.routes.get(normalize_url(str(url)))
        if replies is None:
            reply = Reply(status=404)
        else:
            reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if reply.latency:
            await asyncio.sleep(reply.latency)
        if reply.raises is not None:
            raise reply.raises
        # without reason aiohttp's raise_for_status() fails on an assert
        return CallbackResult(status=reply.status, body=reply.body, headers=reply.headers,
                              content_type=reply.content_type, reason=HTTPStatus(reply.status).phrase)

    @contextmanager
    def running(self):
        with aioresponses() as m:
            m.get(re.compile(r".*"), callback=self._handle, repeat=True)
            yield self

    def urls(self, path_only: bool = False) -> list[str]:
        return [urlsplit(r.url).path if path_only else r.url for r in self.requests]

    def times(self, url: str | None = None, host: str | None = None) -> list[float]:
        return [r.at for r in self.requests
                if (url is None or normalize_url(r.url) == normalize_url(url))
                and (host is None or urlsplit(r.url).hostname == host)]

    def page_requests(self) -> list[Request]:
        return [r for r in self.requests if not r.url.endswith("/robots.txt")]


def intervals(times: list[float]) -> list[float]:
    return [b - a for a, b in zip(times, times[1:])]
