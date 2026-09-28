import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from polite_crawler import AsyncCrawler, PoliteSnapshot
from settings import Config


def print_progress(snap: PoliteSnapshot) -> None:
    print(snap.format(), flush=True)


def save_jsonl(path: Path, pages: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for p in pages:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")


def fmt(value: float | None, unit: str = "s") -> str:
    return f"{value:.2f}{unit}" if value is not None else "-"


async def show_robots(crawler: AsyncCrawler, urls: list[str]) -> None:
    """--check-robots: print the robots.txt rules and whether each URL may be fetched."""
    async with crawler:
        for url in urls:
            rules = await crawler.robots.fetch_robots(url)
            print(f"{rules['url']} (HTTP {rules['status']}): allow_all={rules['allow_all']} "
                  f"disallow_all={rules['disallow_all']}, sitemaps={rules['sitemaps']}")
            for g in rules["groups"]:
                print(f"  User-agent {g['user_agents']}: disallow={g['disallow']} allow={g['allow']} "
                      f"crawl-delay={g['crawl_delay']}")
            for ua in crawler.user_agents.agents:
                verdict = "allowed" if crawler.robots.can_fetch(url, ua) else "BLOCKED"
                print(f"  {url} for {ua!r}: {verdict}; crawl-delay {crawler.robots.get_crawl_delay(ua, url)}")


async def run(args: argparse.Namespace) -> None:
    config = Config(max_retries=args.max_retries)
    crawler = AsyncCrawler(
        max_concurrent=args.workers,
        max_depth=args.max_depth,
        config=config,
        requests_per_second=args.rps,
        per_domain=not args.global_limit,
        min_delay=args.min_delay,
        jitter=args.jitter,
        respect_robots=not args.ignore_robots,
        user_agent=args.user_agent,
        user_agents=args.rotate_ua,
        progress_interval=args.progress_interval,
        on_progress=print_progress,
    )
    if args.check_robots:
        return await show_robots(crawler, args.urls)

    async with crawler:
        results = await crawler.crawl(args.urls, max_pages=args.max_pages,
                                      include_patterns=args.include, exclude_patterns=args.exclude)
    stats = crawler.get_stats()
    await asyncio.to_thread(save_jsonl, args.output, results)

    rate = stats["rate"]
    print(f"\nPages: {len(results)} in {stats['elapsed']:.2f}s; ok: {stats['processed']}, "
          f"errors: {stats['failed']}, blocked by robots.txt: {stats['blocked']}")
    print(f"Requests: {rate['requests']} (robots.txt included), retries: {stats['http'].get('retries', 0)}; "
          f"speed: average {rate['average_rps']:.2f} req/s, current {rate['current_rps']:.2f} req/s")
    print(f"Delays: configured interval {rate['configured_interval']:.2f}s (+ jitter up to {args.jitter}s), "
          f"avg interval {fmt(rate['avg_interval'])}, avg wait in limiter {rate['avg_wait']:.2f}s")
    for domain, d in rate["per_domain"].items():
        delay = rate["crawl_delays"].get(domain)
        print(f"  {domain}: {d['requests']} requests, avg interval {fmt(d['avg_interval'])}, "
              f"avg wait {d['avg_wait']:.2f}s, Crawl-delay {delay if delay is not None else '-'}")
    if crawler.blocked_urls:
        print("Blocked by robots.txt:")
        for url in list(crawler.blocked_urls)[:15]:
            print(f"  - {url}")
        if len(crawler.blocked_urls) > 15:
            print(f"  ... and {len(crawler.blocked_urls) - 15} more")
    for url, error in list(crawler.failed_urls.items())[:10]:
        print(f"  ERR {url}: {error}")
    print(f"Data saved to {args.output}")


def main() -> None:
    p = argparse.ArgumentParser(description="Day 4: polite crawling — rate limits, robots.txt, delays, retries")
    p.add_argument("--urls", nargs="+", required=True, help="start URLs")
    p.add_argument("--max-depth", type=int, default=1)
    p.add_argument("--max-pages", type=int, default=20)
    p.add_argument("--workers", type=int, default=5, help="max concurrent requests")
    p.add_argument("--rps", type=float, default=2.0, help="requests per second (per domain unless --global-limit)")
    p.add_argument("--global-limit", action="store_true", help="one limit for all domains (per_domain=False)")
    p.add_argument("--min-delay", type=float, default=0.0, help="minimum seconds between requests")
    p.add_argument("--jitter", type=float, default=0.0, help="random extra delay, up to this many seconds")
    p.add_argument("--user-agent", default="MyBot/1.0")
    p.add_argument("--rotate-ua", nargs="+", help="rotate these User-Agents (robots.txt must allow all of them)")
    p.add_argument("--ignore-robots", action="store_true")
    p.add_argument("--check-robots", action="store_true", help="only show robots.txt rules for the URLs")
    p.add_argument("--max-retries", type=int, default=Config.max_retries)
    p.add_argument("--include", action="append", help="regex a link must match (repeatable)")
    p.add_argument("--exclude", action="append", help="regex that rejects a link (repeatable)")
    p.add_argument("--progress-interval", type=float, default=1.0)
    p.add_argument("--output", type=Path, default=Path("results.jsonl"))
    p.add_argument("--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = p.parse_args()
    sys.stdout.reconfigure(errors="replace")  # cp1251 Windows console
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
