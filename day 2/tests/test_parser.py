from pathlib import Path

import pytest

from html_parser import DEFAULT_SELECTORS, HTMLParser

FIXTURES = Path(__file__).parent / "fixtures"
INDEX_URL = "http://books.toscrape.com/index.html"
PRODUCT_URL = "http://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def parser() -> HTMLParser:
    return HTMLParser()


# --- extract_links -----------------------------------------------------------

def test_links_on_saved_index_page_are_absolute_and_unique(parser):
    links = parser.extract_links(load("books_index.html"), INDEX_URL)
    assert len(links) == 73
    assert len(set(links)) == len(links)
    assert all(link.startswith("http://books.toscrape.com/") for link in links)
    assert "http://books.toscrape.com/catalogue/category/books/travel_2/index.html" in links
    assert "http://books.toscrape.com/catalogue/page-2.html" in links
    assert "http://books.toscrape.com/catalogue/a-light-in-the-attic_1000/index.html" in links


def test_links_on_saved_product_page_resolve_parent_dirs(parser):
    links = parser.extract_links(load("books_product.html"), PRODUCT_URL)
    assert links == [
        "http://books.toscrape.com/index.html",
        "http://books.toscrape.com/catalogue/category/books_1/index.html",
        "http://books.toscrape.com/catalogue/category/books/poetry_23/index.html",
    ]


def test_link_edge_cases(parser):
    links = parser.extract_links(load("links_edge_cases.html"), "http://site.test/dir/current.html")
    assert links == [
        "http://site.test/about",
        "http://site.test/dir/page.html",
        "http://site.test/up.html",
        "https://other.test/x",
        "http://cdn.test/lib",
        "http://site.test/spaced",
    ]


def test_base_tag_changes_resolution(parser):
    html = '<head><base href="http://cdn.test/root/"></head><a href="a.html">a</a>'
    assert parser.extract_links(html, "http://site.test/x/y.html") == ["http://cdn.test/root/a.html"]


@pytest.mark.parametrize("html", ["", None, "   ", "<html></html>", "just text"])
def test_links_on_empty_page(parser, html):
    assert parser.extract_links(html, "http://site.test/") == []


def test_links_on_broken_html(parser):
    links = parser.extract_links(load("broken.html"), "http://site.test/")
    assert "http://site.test/next" in links
    assert all(link.startswith("http://site.test/") for link in links)


# --- extract_data ------------------------------------------------------------

def test_default_fields_on_saved_index_page(parser):
    data = parser.extract_data(load("books_index.html"))
    assert data == {"title": "All products | Books to Scrape - Sandbox", "description": ""}


def test_custom_selectors_on_saved_product_page(parser):
    selectors = {
        **DEFAULT_SELECTORS,
        "name": "h1",
        "price": ".product_main .price_color",
        "breadcrumbs": {"selector": "ul.breadcrumb a", "all": True},
        "image": {"selector": "#product_gallery img", "attr": "src"},
        "rating": {"selector": ".product_main .star-rating", "attr": "class"},
    }
    data = parser.extract_data(load("books_product.html"), selectors)
    assert data["title"] == "A Light in the Attic | Books to Scrape - Sandbox"
    assert data["description"].startswith("It's hard to imagine a world without A Light in the Attic.")
    assert data["name"] == "A Light in the Attic"
    assert data["price"] == "£51.77"
    assert data["breadcrumbs"] == ["Home", "Books", "Poetry"]
    assert data["image"] == "../../media/cache/fe/72/fe72f0532301ec28892ae79a629a293c.jpg"
    assert data["rating"] == "star-rating Three"


def test_missing_elements_give_none_not_exception(parser):
    selectors = {
        "title": "title",
        "description": 'meta[name="description"]',
        "price": ".price_color",
        "image": {"selector": "img", "attr": "src"},
        "missing_attr": {"selector": "title", "attr": "data-x"},
        "all_missing": {"selector": ".nope", "all": True},
    }
    data = parser.extract_data("<html><body><p>nothing here</p></body></html>", selectors)
    assert data == {
        "title": None,
        "description": None,
        "price": None,
        "image": None,
        "missing_attr": None,
        "all_missing": [],
    }


@pytest.mark.parametrize("html", ["", None, "<<<>>>", "</div></html>"])
def test_data_on_empty_or_garbage_page(parser, html):
    assert parser.extract_data(html) == {"title": None, "description": None}


def test_data_on_broken_html(parser):
    data = parser.extract_data(load("broken.html"), {**DEFAULT_SELECTORS, "price": ".price"})
    assert data["title"].startswith("Broken page")
    assert data["price"].startswith("10.00")


def test_text_whitespace_is_collapsed(parser):
    html = "<title>Poetry |\n        Books to Scrape</title><p>a <b>b</b>\tc</p>"
    assert parser.extract_data(html, {"title": "title", "p": "p"}) == {
        "title": "Poetry | Books to Scrape",
        "p": "a b c",
    }


def test_default_selectors_used_when_none_given(parser):
    html = '<title> Hi </title><meta name="description" content=" About us ">'
    assert parser.extract_data(html) == {"title": "Hi", "description": "About us"}
