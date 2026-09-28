"""Crawler exception hierarchy.

CrawlerError
├── FetchError              network error, timeout or HTTP error status for a URL
│   └── RateLimitExceeded   HTTP 429 (Too Many Requests), with the server's Retry-After
├── ParseError              the page was fetched but could not be parsed
└── RobotsDisallowedError   robots.txt forbids the URL for our User-Agent
"""


class CrawlerError(Exception):
    """Base class: every crawler error carries the URL it happened on."""

    def __init__(self, url: str, message: str):
        super().__init__(message)
        self.url = url


class FetchError(CrawlerError):
    """Failed request. `retryable` tells the retry loop whether another attempt makes sense:
    connection errors, timeouts, 5xx and 429 are retryable; other 4xx are not."""

    def __init__(
        self,
        url: str,
        message: str,
        *,
        status: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
        cause: BaseException | None = None,
    ):
        super().__init__(url, message)
        self.status = status
        self.retryable = retryable
        self.retry_after = retry_after  # seconds the server asked us to wait (Retry-After)
        self.cause = cause


class RateLimitExceeded(FetchError):
    """HTTP 429: the server throttles us; always retryable, honours Retry-After."""

    def __init__(self, url: str, retry_after: float | None = None):
        suffix = f", Retry-After {retry_after:g}s" if retry_after is not None else ""
        super().__init__(url, f"HTTP 429 Too Many Requests{suffix}", status=429, retryable=True, retry_after=retry_after)


class ParseError(CrawlerError):
    def __init__(self, url: str, cause: BaseException):
        super().__init__(url, f"cannot parse page: {type(cause).__name__}: {cause}")
        self.cause = cause


class RobotsDisallowedError(CrawlerError):
    def __init__(self, url: str, user_agent: str):
        super().__init__(url, f"disallowed by robots.txt for {user_agent!r}")
        self.user_agent = user_agent
