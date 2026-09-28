import re

import pytest

from url_filter import UrlFilter, host_of, same_domain


def test_host_of():
    assert host_of("http://Sub.Site.TEST:8080/a") == "sub.site.test"
    assert host_of("not a url") == ""


@pytest.mark.parametrize("url, expected", [
    ("http://site.test/a", True),
    ("https://blog.site.test/", True),  # subdomain
    ("http://notsite.test/", False),  # suffix is not a subdomain
    ("http://site.test.evil/", False),
    ("http://other.test/", False),
])
def test_same_domain(url, expected):
    assert same_domain(url, {"site.test"}) is expected


def test_same_domain_only_toggle():
    assert not UrlFilter({"site.test"}).allows("http://other.test/")
    assert UrlFilter({"site.test"}, same_domain_only=False).allows("http://other.test/")


@pytest.mark.parametrize("url", ["mailto:a@site.test", "javascript:void(0)", "ftp://site.test/f", "tel:123"])
def test_non_http_schemes_rejected(url):
    assert not UrlFilter({"site.test"}, same_domain_only=False).allows(url)


def test_exclude_patterns():
    f = UrlFilter({"site.test"}, exclude_patterns=[r"\.pdf$", "/login", re.compile(r"\?sort=")])
    assert f.allows("http://site.test/catalogue/page-2.html")
    assert not f.allows("http://site.test/doc.pdf")
    assert not f.allows("http://site.test/login?next=/")
    assert not f.allows("http://site.test/list?sort=asc")


def test_include_patterns():
    f = UrlFilter({"site.test"}, include_patterns=["/catalogue/", "/category/"])
    assert f.allows("http://site.test/catalogue/book_1/index.html")
    assert f.allows("http://site.test/category/poetry/")
    assert not f.allows("http://site.test/about")


def test_exclude_wins_over_include():
    f = UrlFilter({"site.test"}, include_patterns=["/catalogue/"], exclude_patterns=["page-"])
    assert f.allows("http://site.test/catalogue/book/")
    assert not f.allows("http://site.test/catalogue/page-2.html")


def test_filter_is_callable():
    assert list(filter(UrlFilter({"site.test"}), ["http://site.test/", "http://x.test/"])) == ["http://site.test/"]
