import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from async_crawler import AsyncCrawler
from crawl_stats import ProgressSnapshot


def print_progress(snap: ProgressSnapshot) -> None:
    print(snap.format(), flush=True)


def save_jsonl(path: Path, pages: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for p in pages:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")


async def run(args: argparse.Namespace) -> None:
    async with AsyncCrawler(
        max_concurrent=args.workers,
        max_depth=args.max_depth,
        max_per_domain=args.max_per_domain,
        requests_per_second=args.rps,
        per_host_rps=args.per_host_rps,
        progress_interval=args.progress_interval,
        on_progress=print_progress,
    ) as crawler:
        results = await crawler.crawl(
            args.urls,
            max_pages=args.max_pages,
            same_domain_only=not args.all_domains,
            include_patterns=args.include,
            exclude_patterns=args.exclude,
        )
        stats = crawler.get_stats()
        problems = crawler.rate_limiter.check_limits()

    await asyncio.to_thread(save_jsonl, args.output, results)

    print(f"\nОбработано: {len(results)} страниц за {stats['elapsed']:.2f} с "
          f"({stats['pages_per_sec']:.2f} стр/с); успешно: {stats['processed']}, ошибок: {stats['failed']}, "
          f"дубликатов отброшено: {stats['duplicates']}")
    by_depth: dict[int, int] = {}
    for r in results:
        by_depth[r["depth"]] = by_depth.get(r["depth"], 0) + 1
    print(f"По глубине: {dict(sorted(by_depth.items()))}")
    conc = stats["concurrency"]
    print(f"Пик одновременных запросов: {conc['peak_active']} (по доменам: {conc['peak_per_domain']})")
    rps = stats["requests_per_sec"]
    fmt = lambda r: f"{r:.2f}" if r is not None else "-"  # noqa: E731
    print(f"Фактический RPS: global={fmt(rps['global'])} (лимит {args.rps}); "
          + ", ".join(f"{h}={fmt(r)}" for h, r in rps["per_host"].items()) + f" (лимит на домен {args.per_host_rps})")
    print("Лимиты RPS соблюдены" if not problems else "ПРЕВЫШЕНИЕ ЛИМИТА: " + "; ".join(problems))
    for url, error in list(crawler.failed_urls.items())[:10]:
        print(f"  ERR {url}: {error}")
    print(f"Данные сохранены в {args.output}")


def main() -> None:
    p = argparse.ArgumentParser(description="Day 3: crawl with queue, depth limit, filters, concurrency and rate limits")
    p.add_argument("--urls", nargs="+", required=True, help="start URLs")
    p.add_argument("--max-depth", type=int, default=2)
    p.add_argument("--max-pages", type=int, default=50)
    p.add_argument("--workers", type=int, default=10, help="max concurrent requests (worker count)")
    p.add_argument("--max-per-domain", type=int, default=None, help="max concurrent requests per domain")
    p.add_argument("--rps", type=float, default=None, help="global requests per second")
    p.add_argument("--per-host-rps", type=float, default=None, help="requests per second for each domain")
    p.add_argument("--all-domains", action="store_true", help="follow links to other domains (same_domain_only=False)")
    p.add_argument("--include", action="append", help="regex a link must match (repeatable)")
    p.add_argument("--exclude", action="append", help="regex that rejects a link (repeatable)")
    p.add_argument("--progress-interval", type=float, default=1.0, help="seconds between progress lines")
    p.add_argument("--output", type=Path, default=Path("results.jsonl"), help="JSON Lines file for page data")
    p.add_argument("--log-level", default="WARNING", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = p.parse_args()
    sys.stdout.reconfigure(errors="replace")  # e.g. "£" on a cp1251 Windows console
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
