import asyncio
import logging
import random
import time
from collections import Counter
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass

import aiohttp

from day3 import AsyncHTTPClient, FetchResult, host_of
from exceptions import CrawlerError, FetchError, RateLimitExceeded
from polite_limiter import RateLimiter
from retry import backoff_delay, is_retryable_status, parse_retry_after
from settings import Config
from user_agents import UserAgentRotator

logger = logging.getLogger(__name__)

SlotFactory = Callable[[str], AbstractAsyncContextManager]


@dataclass
class FetchOutcome(FetchResult):
    attempts: int = 1
    exception: CrawlerError | None = None  # why the URL failed (None on success)


class ResilientHTTPClient(AsyncHTTPClient):
    """Day 1 client + retries, politeness and User-Agent rotation.

    fetch(url) never raises for network/HTTP problems: it returns a FetchOutcome
    with success=False, the final status, the reason and the attempt count.

    Every attempt (retries included) goes through, in this order:
    slot(url) (concurrency limit, optional) -> rate_limiter.acquire(domain) -> request
    with the next User-Agent. Retried: connection errors, timeouts, 5xx, 429; not
    retried: other 4xx and other client errors. Backoff comes from retry.backoff_delay,
    or from Retry-After (429/503), which also pauses the whole domain in the rate
    limiter so parallel workers back off too. The slot is released while sleeping.
    """

    def __init__(
        self,
        config: Config | None = None,
        *,
        rate_limiter: RateLimiter | None = None,
        user_agents: UserAgentRotator | None = None,
        rng: random.Random | None = None,
    ):
        super().__init__(config or Config())
        self.rate_limiter = rate_limiter
        self.user_agents = user_agents or UserAgentRotator(self.config.user_agent)
        self.rng = rng or random.Random()
        self.counters: Counter[str] = Counter()  # attempts, retries, succeeded, failed

    async def fetch(self, url: str, *, slot: SlotFactory | None = None) -> FetchOutcome:
        if self._session is None:
            raise RuntimeError("client is not started: use it as an async context manager or call start()")
        start = time.perf_counter()
        attempts = 0
        error: FetchError | None = None
        try:
            async with asyncio.timeout(self.config.operation_timeout):
                while True:
                    attempts += 1
                    try:
                        outcome = await self._attempt(url, attempts, slot)
                    except FetchError as e:
                        error = e
                        if not e.retryable:
                            logger.warning("Failed %s (attempt %d): %s - not retryable", url, attempts, e)
                            break
                        if attempts > self.config.max_retries:
                            logger.warning("Failed %s (attempt %d): %s - retries exhausted", url, attempts, e)
                            break
                        await self._wait_before_retry(url, attempts, e)
                    else:
                        outcome.elapsed = time.perf_counter() - start
                        self.counters["succeeded"] += 1
                        if attempts > 1:
                            logger.info("Fetched %s on attempt %d", url, attempts)
                        return outcome
        except TimeoutError:
            error = FetchError(url, f"operation timeout {self.config.operation_timeout:g}s exceeded"
                                    + (f" (last error: {error})" if error else ""),
                               status=error.status if error else None, cause=error)
            logger.warning("Failed %s (attempt %d): %s", url, attempts, error)

        self.counters["failed"] += 1
        return FetchOutcome(
            url=url, success=False, status=error.status, elapsed=time.perf_counter() - start,
            error=f"{type(error).__name__}: {error} (attempts: {attempts})", attempts=attempts, exception=error,
        )

    async def _wait_before_retry(self, url: str, attempt: int, error: FetchError) -> None:
        if error.retry_after is not None:
            delay = min(error.retry_after, self.config.max_retry_after)
        else:
            delay = backoff_delay(attempt, self.config, self.rng)
        if self.rate_limiter is not None and (error.retry_after is not None or isinstance(error, RateLimitExceeded)):
            self.rate_limiter.pause(host_of(url), delay)  # the server asked the whole site to slow down
        self.counters["retries"] += 1
        logger.warning("Retry %d/%d for %s in %.2fs: %s", attempt, self.config.max_retries, url, delay, error)
        await asyncio.sleep(delay)

    async def _attempt(self, url: str, attempt: int, slot: SlotFactory | None) -> FetchOutcome:
        async with slot(url) if slot is not None else nullcontext():
            if self.rate_limiter is not None:
                await self.rate_limiter.acquire(host_of(url))
            user_agent = self.user_agents.next()
            self.counters["attempts"] += 1
            logger.info("Fetching %s (attempt %d, UA %r)", url, attempt, user_agent)
            try:
                async with self._session.get(url, headers={"User-Agent": user_agent}) as resp:
                    if resp.status == 429:
                        raise RateLimitExceeded(url, parse_retry_after(resp.headers.get("Retry-After")))
                    if resp.status >= 400:
                        retry_after = parse_retry_after(resp.headers.get("Retry-After")) if resp.status == 503 else None
                        raise FetchError(url, f"HTTP {resp.status} {resp.reason or ''}".rstrip(), status=resp.status,
                                         retryable=is_retryable_status(resp.status), retry_after=retry_after)
                    body = await resp.text(errors="replace")
                    return FetchOutcome(url=url, success=True, status=resp.status, body=body,
                                        headers=dict(resp.headers), attempts=attempt)
            except FetchError:
                raise
            except aiohttp.ClientConnectionError as e:  # includes aiohttp's socket/connect timeouts
                raise FetchError(url, f"{type(e).__name__}: {e}", retryable=True, cause=e) from e
            except TimeoutError as e:  # ClientTimeout(total=...) of one attempt
                raise FetchError(url, f"timeout: {type(e).__name__}", retryable=True, cause=e) from e
            except aiohttp.ClientError as e:  # bad URL, too many redirects, payload errors...
                raise FetchError(url, f"{type(e).__name__}: {e}", retryable=False, cause=e) from e
