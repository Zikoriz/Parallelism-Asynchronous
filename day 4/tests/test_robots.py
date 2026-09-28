import asyncio

import pytest

from mock_server import MockServer, Reply
from resilient_client import ResilientHTTPClient
from robots import RobotsParser, agent_token, origin_of, parse_robots
from settings import Config

ROBOTS = """
# comment line
User-agent: *
Disallow: /private/
Allow: /private/public/
Disallow: /*.pdf$
Disallow: /search?     # query strings of /search
Crawl-delay: 1.5

User-agent: MyBot
User-agent: OtherBot
Disallow: /mybot-only/
Crawl-delay: 3

User-agent: BadBot
Disallow: /

Sitemap: http://site.test/sitemap.xml
"""


@pytest.fixture
def rules():
    return parse_robots(ROBOTS)


@pytest.mark.parametrize("path, allowed", [
    ("/", True),
    ("/catalogue/page-1.html", True),
    ("/private/", False),
    ("/private/secret.html", False),
    ("/private/public/ok.html", True),  # longer Allow beats shorter Disallow
    ("/docs/file.pdf", False),
    ("/docs/file.pdf?x=1", True),  # "$" anchors the end
    ("/search?q=books", False),
    ("/search", True),
    ("/robots.txt", True),
])
def test_star_group_rules(rules, path, allowed):
    assert rules.can_fetch(f"http://site.test{path}", "*") is allowed


def test_specific_group_replaces_star_group(rules):
    ua = "MyBot/1.0 (+http://mybot.test)"
    assert not rules.can_fetch("http://site.test/mybot-only/x", ua)
    assert rules.can_fetch("http://site.test/private/secret.html", ua)  # "*" rules don't apply to MyBot
    assert rules.can_fetch("http://site.test/mybot-only/x", "SomeoneElse/2.0")
    assert not rules.can_fetch("http://site.test/anything", "BadBot/0.1")
    assert rules.can_fetch("http://site.test/mybot-only/x", "MyBotExtended/1")  # token must match exactly


def test_crawl_delay_per_agent(rules):
    assert rules.crawl_delay("*") == 1.5
    assert rules.crawl_delay("MyBot/1.0") == 3.0
    assert rules.crawl_delay("otherbot") == 3.0
    assert rules.crawl_delay("BadBot") is None


def test_sitemaps_and_dict(rules):
    d = rules.as_dict()
    assert d["sitemaps"] == ["http://site.test/sitemap.xml"]
    star = d["groups"][0]
    assert star == {
        "user_agents": ["*"],
        "allow": ["/private/public/"],
        "disallow": ["/private/", "/*.pdf$", "/search?"],
        "crawl_delay": 1.5,
    }
    assert d["groups"][1]["user_agents"] == ["mybot", "otherbot"]


def test_tie_between_allow_and_disallow_allows():
    rules = parse_robots("User-agent: *\nDisallow: /page\nAllow: /page\n")
    assert rules.can_fetch("http://s.test/page")


def test_empty_disallow_and_no_groups():
    assert parse_robots("User-agent: *\nDisallow:\n").can_fetch("http://s.test/x")
    assert parse_robots("").can_fetch("http://s.test/x")
    assert parse_robots("garbage\n<<>>\nDisallow: /x").can_fetch("http://s.test/x")  # rule outside a group


def test_merged_groups_for_same_agent():
    rules = parse_robots("User-agent: a\nDisallow: /one\n\nUser-agent: A\nDisallow: /two\n")
    assert not rules.can_fetch("http://s/one", "a") and not rules.can_fetch("http://s/two", "a")


def test_helpers():
    assert agent_token("MyBot/1.0 (+http://x)") == "mybot"
    assert agent_token("*") == "*"
    assert origin_of("HTTPS://Site.Test:8443/a/b?c") == "https://site.test:8443"


# --- fetching and caching ----------------------------------------------------

def load(run_virtual, routes, urls, parser_ua="*"):
    server = MockServer(routes)

    async def scenario():
        with server.running():
            async with ResilientHTTPClient(Config(max_retries=1, backoff_jitter=0.0)) as client:
                parser = RobotsParser(client, parser_ua)
                results = await asyncio.gather(*(parser.fetch_robots(u) for u in urls))
                return parser, results

    parser, results = run_virtual(scenario())
    return parser, results, server


def test_fetch_is_cached_per_domain(run_virtual):
    parser, results, server = load(
        run_virtual,
        {"http://site.test/robots.txt": ROBOTS, "http://other.test/robots.txt": "User-agent: *\nDisallow: /x"},
        ["http://site.test/", "http://site.test/a", "http://site.test/b?c", "http://other.test/"],
    )
    assert sorted(server.urls()) == ["http://other.test/robots.txt", "http://site.test/robots.txt"]
    assert parser.fetch_count == 2
    assert results[0] == results[1] == results[2]
    assert results[0]["status"] == 200 and results[0]["url"] == "http://site.test/robots.txt"
    assert not parser.can_fetch("http://site.test/private/x")
    assert not parser.can_fetch("http://other.test/x/y")
    assert parser.can_fetch("http://site.test/private/x", "MyBot/1.0")


def test_missing_robots_allows_everything(run_virtual):
    parser, results, _ = load(run_virtual, {}, ["http://site.test/"])  # 404
    assert results[0]["allow_all"] and parser.can_fetch("http://site.test/private/")


def test_unreachable_robots_disallows_everything(run_virtual):
    parser, results, server = load(run_virtual, {"http://site.test/robots.txt": Reply(503)}, ["http://site.test/"])
    assert len(server.requests) == 2  # retried once, then given up
    assert results[0]["disallow_all"] and not parser.can_fetch("http://site.test/")


def test_get_crawl_delay(run_virtual):
    parser, _, _ = load(
        run_virtual,
        {"http://site.test/robots.txt": ROBOTS, "http://other.test/robots.txt": "User-agent: *\nCrawl-delay: 7"},
        ["http://site.test/", "http://other.test/"],
    )
    assert parser.get_crawl_delay("*", "http://site.test/x") == 1.5
    assert parser.get_crawl_delay("MyBot/1.0", "http://site.test/x") == 3.0
    assert parser.get_crawl_delay("*", "http://other.test/") == 7.0
    assert parser.get_crawl_delay("*") == 7.0  # most recently requested domain
    assert parser.get_crawl_delay("*", "http://unknown.test/") == 0.0


def test_can_fetch_before_fetch_is_permissive():
    parser = RobotsParser(client=None)
    assert parser.can_fetch("http://never-fetched.test/private/") and not parser.is_cached("http://never-fetched.test/")
