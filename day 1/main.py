import argparse
import asyncio
import logging
import time

from client import AsyncHTTPClient, FetchResult, Fetcher
from config import Config


async def fetch_sequential(client: AsyncHTTPClient, urls: list[str]) -> list[FetchResult]:
    return [await client.fetch(url) for url in urls]


def print_results(title: str, results: list[FetchResult], total: float) -> None:
    print(f"\n{title}: {len(results)} URLs in {total:.2f} s")
    for r in results:
        if r.success:
            print(f"  {r.status} {r.elapsed * 1000:7.0f} ms  {r.url}")
        else:
            status = r.status if r.status is not None else "ERR"
            print(f"  {status} {r.elapsed * 1000:7.0f} ms  {r.url}  ({r.error})")


async def run(urls: list[str], config: Config) -> None:
    async with AsyncHTTPClient(config) as client:
        start = time.perf_counter()
        sequential = await fetch_sequential(client, urls)
        seq_total = time.perf_counter() - start

        start = time.perf_counter()
        parallel = await Fetcher(client, config).fetch_many(urls)
        par_total = time.perf_counter() - start

    print_results("Sequential", sequential, seq_total)
    print_results(f"Parallel (max_concurrency={config.max_concurrency})", parallel, par_total)
    speedup = seq_total / par_total if par_total > 0 else float("inf")
    print(f"\nSequential {seq_total:.2f} s vs parallel {par_total:.2f} s -> {speedup:.1f}x faster")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--urls", nargs="+", required=True)
    p.add_argument("--mode", choices=["fetch-only"], default="fetch-only")
    p.add_argument("--max-concurrency", type=int, default=Config.max_concurrency)
    p.add_argument("--timeout", type=float, default=Config.total_timeout, help="total request timeout, s")
    p.add_argument("--connect-timeout", type=float, default=Config.connect_timeout)
    p.add_argument("--read-timeout", type=float, default=Config.read_timeout)
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    config = Config(
        max_concurrency=args.max_concurrency,
        total_timeout=args.timeout,
        connect_timeout=args.connect_timeout,
        read_timeout=args.read_timeout,
    )
    asyncio.run(run(args.urls, config))


if __name__ == "__main__":
    main()
