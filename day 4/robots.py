"""robots.txt fetching, parsing (RFC 9309) and per-domain caching.

urllib.robotparser is not used: it has no `*`/`$` wildcards and applies the first
matching rule instead of the most specific one.
"""
import asyncio
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from day3 import FetchResult

logger = logging.getLogger(__name__)

MAX_ROBOTS_SIZE = 500 * 1024  # RFC 9309: parse at least the first 500 KiB


def agent_token(user_agent: str) -> str:
    """"MyBot/1.0 (+http://x)" -> "mybot": robots.txt groups match the product token."""
    token = user_agent.strip().split("/")[0].split()[0] if user_agent.strip() else "*"
    return token.lower()


def origin_of(url: str) -> str:
    """scheme://host[:port] — robots.txt applies per origin."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), "", "", ""))


def _path_of(url: str) -> str:
    parts = urlsplit(url)
    return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")


@dataclass(frozen=True)
class Rule:
    allow: bool
    pattern: str

    def regex(self) -> re.Pattern:
        anchored = self.pattern.endswith("$")
        body = self.pattern[:-1] if anchored else self.pattern
        return re.compile(".*".join(re.escape(part) for part in body.split("*")) + ("$" if anchored else ""))

    def matches(self, path: str) -> bool:
        return self.regex().match(path) is not None


@dataclass
class Group:
    agents: list[str] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    crawl_delay: float | None = None


@dataclass
class RobotsRules:
    """Parsed robots.txt of one origin.

    allow_all / disallow_all short-circuit the rules: 4xx robots.txt means "no
    restrictions", an unreachable one (5xx, network error) means "crawl nothing"
    (RFC 9309, section 2.3.1).
    """

    groups: list[Group] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)
    allow_all: bool = False
    disallow_all: bool = False
    url: str | None = None
    status: int | None = None

    def group_for(self, user_agent: str) -> Group | None:
        """Rules of the group naming our product token (all such groups merged), else the `*` group."""
        token = agent_token(user_agent)
        for wanted in (token, "*"):
            matching = [g for g in self.groups if wanted in g.agents]
            if matching:
                merged = Group(agents=[wanted])
                for g in matching:
                    merged.rules.extend(g.rules)
                    if g.crawl_delay is not None:
                        merged.crawl_delay = max(merged.crawl_delay or 0.0, g.crawl_delay)
                return merged
        return None

    def can_fetch(self, url: str, user_agent: str = "*") -> bool:
        if self.allow_all:
            return True
        if self.disallow_all:
            return False
        path = _path_of(url)
        if path == "/robots.txt":
            return True
        group = self.group_for(user_agent)
        if group is None:
            return True
        # the most specific (longest) matching rule wins; on a tie Allow wins
        best: Rule | None = None
        for rule in group.rules:
            if rule.matches(path) and (
                best is None or len(rule.pattern) > len(best.pattern)
                or (len(rule.pattern) == len(best.pattern) and rule.allow)
            ):
                best = rule
        return best is None or best.allow

    def crawl_delay(self, user_agent: str = "*") -> float | None:
        group = self.group_for(user_agent)
        return group.crawl_delay if group else None

    def as_dict(self) -> dict:
        return {
            "url": self.url,
            "status": self.status,
            "allow_all": self.allow_all,
            "disallow_all": self.disallow_all,
            "groups": [
                {
                    "user_agents": g.agents,
                    "allow": [r.pattern for r in g.rules if r.allow],
                    "disallow": [r.pattern for r in g.rules if not r.allow],
                    "crawl_delay": g.crawl_delay,
                }
                for g in self.groups
            ],
            "sitemaps": self.sitemaps,
        }


def parse_robots(text: str) -> RobotsRules:
    """Parse robots.txt content. Unknown lines and rules outside a group are ignored."""
    rules = RobotsRules()
    current: Group | None = None
    group_has_rules = False
    for raw in text[:MAX_ROBOTS_SIZE].splitlines():
        line = raw.split("#", 1)[0].strip()
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if current is None or group_has_rules:  # a user-agent after rules starts a new group
                current = Group()
                rules.groups.append(current)
                group_has_rules = False
            current.agents.append(agent_token(value) if value != "*" else "*")
        elif key in ("allow", "disallow"):
            if current is None:
                continue
            group_has_rules = True
            if value:  # "Disallow:" (empty) allows everything, i.e. adds no rule
                current.rules.append(Rule(allow=key == "allow", pattern=value))
        elif key == "crawl-delay":
            if current is None:
                continue
            group_has_rules = True
            try:
                delay = float(value)
            except ValueError:
                continue
            if delay >= 0:
                current.crawl_delay = delay
        elif key == "sitemap" and value:
            rules.sitemaps.append(value)
    return rules


class RobotsParser:
    """Loads robots.txt through `client` (anything with `async fetch(url) -> FetchResult`)
    and caches the rules per origin; concurrent requests for one origin share one download.

    - fetch_robots(base_url) -> dict: rules of the URL's origin (fetched once).
    - can_fetch(url, user_agent): uses the cache; an origin not fetched yet is
      reported as allowed (call fetch_robots first — the crawler always does).
    - get_crawl_delay(user_agent, url=None): Crawl-delay of the URL's origin, or of the
      most recently fetched origin when no URL is given; 0.0 if there is none.
    """

    def __init__(self, client, user_agent: str = "*"):
        self.client = client
        self.user_agent = user_agent
        self._cache: dict[str, RobotsRules] = {}
        self._pending: dict[str, asyncio.Task] = {}
        self._last_origin: str | None = None
        self.fetch_count = 0

    async def fetch_robots(self, base_url: str) -> dict:
        return (await self.get_rules(base_url)).as_dict()

    async def get_rules(self, url: str) -> RobotsRules:
        origin = origin_of(url)
        self._last_origin = origin
        if origin in self._cache:
            return self._cache[origin]
        task = self._pending.get(origin)
        if task is None:
            task = self._pending[origin] = asyncio.create_task(self._download(origin))
        try:
            rules = await asyncio.shield(task)  # one waiter being cancelled must not cancel the download
        finally:
            if task.done():
                self._pending.pop(origin, None)
        self._cache[origin] = rules
        return rules

    async def _download(self, origin: str) -> RobotsRules:
        robots_url = f"{origin}/robots.txt"
        self.fetch_count += 1
        result: FetchResult = await self.client.fetch(robots_url)
        if result.success:
            rules = parse_robots(result.body)
            logger.info("robots.txt %s: %d group(s), %d sitemap(s)", robots_url, len(rules.groups), len(rules.sitemaps))
        elif result.status is not None and 400 <= result.status < 500 and result.status != 429:
            rules = RobotsRules(allow_all=True)
            logger.info("robots.txt %s: HTTP %d, no restrictions", robots_url, result.status)
        else:
            rules = RobotsRules(disallow_all=True)
            logger.warning("robots.txt %s unreachable (%s): the site is treated as fully disallowed",
                           robots_url, result.error)
        rules.url, rules.status = robots_url, result.status
        return rules

    def is_cached(self, url: str) -> bool:
        return origin_of(url) in self._cache

    def cached_rules(self, url: str) -> RobotsRules | None:
        return self._cache.get(origin_of(url))

    def can_fetch(self, url: str, user_agent: str = "*") -> bool:
        rules = self.cached_rules(url)
        return True if rules is None else rules.can_fetch(url, user_agent)

    def can_fetch_all(self, url: str, user_agents: Iterable[str]) -> bool:
        """Allowed for every one of user_agents (used with User-Agent rotation)."""
        return all(self.can_fetch(url, ua) for ua in user_agents)

    def get_crawl_delay(self, user_agent: str = "*", url: str | None = None) -> float:
        origin = origin_of(url) if url else self._last_origin
        rules = self._cache.get(origin) if origin else None
        return (rules.crawl_delay(user_agent) or 0.0) if rules else 0.0
