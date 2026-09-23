from aioresponses import aioresponses
from yarl import URL

from crawler import Crawler
from day1 import AsyncHTTPClient, Config
from queue_manager import normalize_url

ROOT = "http://site.test/"


def page(title: str, *hrefs: str) -> str:
    links = "".join(f'<a href="{h}">{h}</a>' for h in hrefs)
    return f"<html><head><title>{title}</title></head><body>{links}</body></html>"


# Small site with a cycle (/ <-> /a), trailing-slash/fragment variants of the same
# page, an external link, a non-HTTP link, a 404 and a chain deeper than max_depth.
SITE = {
    ROOT: page("home", "/a", "/b", "/a/", "/#top", "http://other.test/", "mailto:x@site.test"),
    "http://site.test/a": page("a", "/", "/b", "/a/deep", "/a#frag"),
    "http://site.test/b": page("b", "/a/", "/c"),
    "http://site.test/a/deep": page("deep", "/a/deep/deeper"),
    "http://site.test/a/deep/deeper": page("deeper"),
}


def mock_site(m: aioresponses, site: dict[str, str]) -> None:
    for url, body in site.items():
        m.get(url, body=body, content_type="text/html", repeat=True)


def requested(m: aioresponses) -> list[str]:
    return [str(url) for (method, url), calls in m.requests.items() for _ in calls]


async def crawl(site: dict[str, str], seeds: list[str], **kwargs):
    with aioresponses() as m:
        mock_site(m, site)
        m.get("http://site.test/c", status=404, repeat=True)
        async with AsyncHTTPClient(Config(max_concurrency=4)) as client:
            pages = await Crawler(client, **kwargs).crawl(seeds)
        return pages, requested(m)


async def test_crawl_respects_depth_without_duplicates_or_cycles():
    pages, calls = await crawl(SITE, [ROOT], max_depth=2)

    by_url = {p.url: p for p in pages}
    assert set(by_url) == {
        ROOT,
        "http://site.test/a",
        "http://site.test/b",
        "http://site.test/a/deep",
        "http://site.test/c",
    }
    assert {u: p.depth for u, p in by_url.items()} == {
        ROOT: 0,
        "http://site.test/a": 1,
        "http://site.test/b": 1,
        "http://site.test/a/deep": 2,
        "http://site.test/c": 2,
    }
    # every page fetched exactly once, nothing outside the seed domain or beyond max_depth
    assert len(calls) == len(set(calls)) == 5
    assert len({normalize_url(u) for u in calls}) == 5
    assert not any("other.test" in u or "deeper" in u for u in calls)


async def test_crawl_collects_data_links_and_errors():
    pages, _ = await crawl(SITE, [ROOT], max_depth=2)
    by_url = {p.url: p for p in pages}

    home = by_url[ROOT]
    assert home.success and home.status == 200
    assert home.data == {"title": "home", "description": None}
    assert "http://other.test/" in home.links  # extracted, but filtered by the queue

    missing = by_url["http://site.test/c"]
    assert not missing.success and missing.status == 404
    assert "ClientResponseError" in missing.error


async def test_crawl_depth_zero_fetches_only_seeds():
    pages, calls = await crawl(SITE, [ROOT, "http://site.test/b"], max_depth=0)
    assert {p.url for p in pages} == {ROOT, "http://site.test/b"}
    assert all(p.depth == 0 and p.success for p in pages)
    assert len(calls) == 2


async def test_crawl_stops_at_max_pages():
    wide = {ROOT: page("hub", *(f"/p{i}" for i in range(20)))}
    wide.update({f"http://site.test/p{i}": page(f"p{i}", "/") for i in range(20)})
    pages, calls = await crawl(wide, [ROOT], max_depth=3, max_pages=5)
    assert len(pages) == 5
    assert len(calls) == 5


async def test_crawl_duplicate_seeds_fetched_once():
    pages, calls = await crawl(SITE, [ROOT, "http://site.test", "http://SITE.test/#x"], max_depth=0)
    assert len(pages) == 1 and len(calls) == 1


async def test_allowed_domains_override_and_custom_selectors():
    site = {
        ROOT: page("home", "http://other.test/"),
        "http://other.test/": page("other") + '<p class="price">9.99</p>',
    }
    pages, calls = await crawl(
        site,
        [ROOT],
        max_depth=1,
        allowed_domains=["site.test", "other.test"],
        selectors={"title": "title", "price": ".price"},
    )
    by_url = {p.url: p for p in pages}
    assert by_url["http://other.test/"].data == {"title": "other", "price": "9.99"}
    assert by_url[ROOT].data == {"title": "home", "price": None}
    assert URL("http://other.test/") in {URL(u) for u in calls}


async def test_non_html_response_is_not_parsed():
    with aioresponses() as m:
        m.get(ROOT, body=page("home", "/img.png"), content_type="text/html")
        m.get("http://site.test/img.png", body=b"\x89PNG<a href='/x'>", content_type="image/png")
        async with AsyncHTTPClient() as client:
            pages = await Crawler(client, max_depth=3).crawl([ROOT])
    img = next(p for p in pages if p.url.endswith("img.png"))
    assert img.success and img.links == [] and img.data == {}
    assert len(pages) == 2
