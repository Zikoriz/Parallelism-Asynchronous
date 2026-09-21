import argparse
import asyncio

from client import AsyncHTTPClient, Fetcher
from config import Config


async def run(urls: list[str], config: Config) -> None:
    async with AsyncHTTPClient(config) as client:
        results = await Fetcher(client, config).fetch_many(urls)
    for r in results:
        if r.success:
            print(f"{r.status} {r.elapsed * 1000:7.0f} ms  {r.url}")
        else:
            print(f"ERR {r.elapsed * 1000:7.0f} ms  {r.url}  ({r.error})")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--urls", nargs="+", required=True)
    p.add_argument("--mode", choices=["fetch-only"], default="fetch-only")
    p.add_argument("--max-concurrency", type=int, default=Config.max_concurrency)
    p.add_argument("--timeout", type=float, default=Config.total_timeout)
    args = p.parse_args()
    config = Config(max_concurrency=args.max_concurrency, total_timeout=args.timeout)
    asyncio.run(run(args.urls, config))


if __name__ == "__main__":
    main()
