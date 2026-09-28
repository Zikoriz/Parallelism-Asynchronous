import re
from collections.abc import Iterable
from urllib.parse import urlsplit

ALLOWED_SCHEMES = ("http", "https")

Pattern = str | re.Pattern


def host_of(url: str) -> str:
    """Lowercased host of a URL: the key for per-domain semaphores and rate limits."""
    return (urlsplit(url).hostname or "").lower()


def same_domain(url: str, domains: Iterable[str]) -> bool:
    """True if the URL's host is one of domains or a subdomain of one."""
    host = host_of(url)
    return any(host == d or host.endswith("." + d) for d in domains)


class UrlFilter:
    """Decides whether a discovered link may be enqueued.

    - only http(s) URLs pass;
    - same_domain_only: the host must be one of `domains` (or a subdomain);
    - exclude_patterns: a URL matching any of them is rejected (checked first);
    - include_patterns: if given, the URL must match at least one of them.

    Patterns are regular expressions (str or compiled) applied with re.search to the
    full URL, so "/tag/" or r"\\.pdf$" work as expected.
    """

    def __init__(
        self,
        domains: Iterable[str] = (),
        *,
        same_domain_only: bool = True,
        include_patterns: Iterable[Pattern] | None = None,
        exclude_patterns: Iterable[Pattern] | None = None,
    ):
        self.domains = {d.lower() for d in domains}
        self.same_domain_only = same_domain_only
        self.include_patterns = [re.compile(p) for p in include_patterns or ()]
        self.exclude_patterns = [re.compile(p) for p in exclude_patterns or ()]

    def allows(self, url: str) -> bool:
        if urlsplit(url).scheme.lower() not in ALLOWED_SCHEMES:
            return False
        if self.same_domain_only and not same_domain(url, self.domains):
            return False
        if any(p.search(url) for p in self.exclude_patterns):
            return False
        if self.include_patterns and not any(p.search(url) for p in self.include_patterns):
            return False
        return True

    __call__ = allows
