import argparse
import asyncio
import logging
import sys
import time
from collections import Counter

from crawler import Crawler
from day1 import AsyncHTTPClient, Config
from html_parser import DEFAULT_SELECTORS, HTMLParser
from queue_manager import normalize_url


def parse_selector(value: str) -> tuple[str, str]:
    name, sep, css = value.partition("=")
    if not sep or not name or not css:
        raise argparse.ArgumentTypeError(f"expected NAME=CSS, got {value!r}")
    return name, css


async def run(args: argparse.Namespace) -> None:
    config = Config(max_concurrency=args.workers)
    selectors = {**DEFAULT_SELECTORS, **dict(args.select)}
    start = time.perf_counter()
    async with AsyncHTTPClient(config) as client:
        crawler = Crawler(
            client,
            HTMLParser(),
            max_depth=args.max_depth,
            max_pages=args.max_pages,
            allowed_domains=args.domain,
            selectors=selectors,
        )
        pages = await crawler.crawl(args.urls)
    elapsed = time.perf_counter() - start

    for p in sorted(pages, key=lambda p: (p.depth, p.url)):
        if p.success:
            fields = "  ".join(f"{k}={v!r}" for k, v in p.data.items() if k != "description")
            print(f"[d{p.depth}] {p.status} {p.url}  links={len(p.links)}  {fields}")
        else:
            print(f"[d{p.depth}] {p.status or 'ERR'} {p.url}  ({p.error})")

    keys = Counter(normalize_url(p.url) for p in pages)
    duplicates = {k: n for k, n in keys.items() if n > 1}
    by_depth = Counter(p.depth for p in pages)
    print(f"\n{len(pages)} pages in {elapsed:.2f} s; by depth: {dict(sorted(by_depth.items()))}; "
          f"failed: {sum(not p.success for p in pages)}; duplicates: {len(duplicates)}")


def main() -> None:
    p = argparse.ArgumentParser(description="Day 2: crawl seeds, extract links and data")
    p.add_argument("--urls", nargs="+", required=True, help="seed URLs")
    p.add_argument("--max-depth", type=int, default=2)
    p.add_argument("--max-pages", type=int, default=50)
    p.add_argument("--workers", type=int, default=Config.max_concurrency)
    p.add_argument("--domain", action="append", help="allowed domain (repeatable); default: seed domains")
    p.add_argument("--select", action="append", type=parse_selector, default=[],
                   help="extra field NAME=CSS (repeatable), e.g. price=.price_color")
    p.add_argument("--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = p.parse_args()
    sys.stdout.reconfigure(errors="replace")  # e.g. "£" on a cp1251 Windows console
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
