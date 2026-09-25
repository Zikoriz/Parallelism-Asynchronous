from pathlib import Path

import pytest
from bs4 import BeautifulSoup

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


# --- parse_html / soup-based interface ----------------------------------------

async def test_parse_html_returns_soup_usable_by_every_extractor(parser):
    soup = await parser.parse_html(load("books_product.html"))
    assert isinstance(soup, BeautifulSoup)
    assert parser.extract_links(soup, PRODUCT_URL) == parser.extract_links(load("books_product.html"), PRODUCT_URL)
    assert parser.extract_data(soup)["title"] == "A Light in the Attic | Books to Scrape - Sandbox"


@pytest.mark.parametrize("html", ["", None, "<<<>>>", "</div></html>"])
async def test_parse_html_on_empty_or_garbage_page(parser, html):
    soup = await parser.parse_html(html)
    assert isinstance(parser.extract_text(soup), str)
    assert parser.extract_links(soup, "http://site.test/") == []
    assert parser.extract_tables(soup) == [] and parser.extract_lists(soup) == []


# --- extract_text ------------------------------------------------------------

def test_text_skips_scripts_styles_comments_and_doctype(parser):
    html = """<!DOCTYPE html><html><head><title>T</title><style>p{color:red}</style>
    <script>var x = "<b>no</b>";</script></head><body><!-- hidden --><h1>Hello</h1>
    <p>world\n  and <b>more</b></p><noscript>enable js</noscript></body></html>"""
    assert parser.extract_text(html) == "Hello world and more"


def test_text_on_saved_product_page(parser):
    text = parser.extract_text(load("books_product.html"))
    assert text.startswith("Books to Scrape We love being scraped! Home Books Poetry A Light in the Attic")
    assert "£51.77" in text and "Product Information" in text
    assert "html" not in text.split()[:3]  # doctype is not text
    assert "\n" not in text and "  " not in text


def test_text_on_broken_html_with_unclosed_head(parser):
    assert parser.extract_text(load("broken.html")) == "10.00 next page other unterminated paragraph"


@pytest.mark.parametrize("html", ["", None, "   ", "<html></html>"])
def test_text_on_empty_page(parser, html):
    assert parser.extract_text(html) == ""


# --- extract_metadata --------------------------------------------------------

def test_metadata_on_saved_product_page(parser):
    meta = parser.extract_metadata(load("books_product.html"))
    assert meta["title"] == "A Light in the Attic | Books to Scrape - Sandbox"
    assert meta["description"].startswith("It's hard to imagine a world without A Light in the Attic.")
    assert meta["keywords"] == []
    assert meta["language"] == "en-us"


def test_metadata_keywords_canonical_and_case_insensitive_names(parser):
    html = """<html lang="uk"><head><title> Shop </title>
    <meta name="Description" content=" Best shop ">
    <meta name="KEYWORDS" content="books, python , ,async">
    <link rel="canonical" href="https://shop.test/"></head></html>"""
    assert parser.extract_metadata(html) == {
        "title": "Shop",
        "description": "Best shop",
        "keywords": ["books", "python", "async"],
        "canonical": "https://shop.test/",
        "language": "uk",
    }


@pytest.mark.parametrize("html", ["", None, "<<<>>>"])
def test_metadata_on_empty_page(parser, html):
    assert parser.extract_metadata(html) == {
        "title": None, "description": None, "keywords": [], "canonical": None, "language": None,
    }


# --- images, headings, tables, lists -----------------------------------------

def test_images_are_absolute_with_alt(parser):
    assert parser.extract_images(load("books_product.html"), PRODUCT_URL) == [{
        "src": "http://books.toscrape.com/media/cache/fe/72/fe72f0532301ec28892ae79a629a293c.jpg",
        "alt": "A Light in the Attic",
    }]
    images = parser.extract_images(load("books_index.html"), INDEX_URL)
    assert len(images) == 20
    assert all(i["src"].startswith("http://books.toscrape.com/media/") for i in images)


def test_images_skip_empty_src_and_honour_base(parser):
    html = '<base href="http://cdn.test/"><img src="a.png"><img src=" "><img alt="x"><img src="b.png" alt="">'
    assert parser.extract_images(html, "http://site.test/") == [
        {"src": "http://cdn.test/a.png", "alt": None},
        {"src": "http://cdn.test/b.png", "alt": ""},
    ]


def test_headings(parser):
    assert parser.extract_headings(load("books_product.html")) == {
        "h1": ["A Light in the Attic"],
        "h2": ["Product Description", "Product Information"],
        "h3": [],
    }
    index = parser.extract_headings(load("books_index.html"))
    assert index["h1"] == ["All products"] and len(index["h3"]) == 20
    assert parser.extract_headings("<h1> </h1><h4>no</h4>") == {"h1": [], "h2": [], "h3": []}


def test_key_value_table_on_saved_product_page(parser):
    [table] = parser.extract_tables(load("books_product.html"))
    assert table["headers"] == []
    assert table["rows"][0] == ["UPC", "a897fe39b1053632"]
    assert ["Price (excl. tax)", "£51.77"] in table["rows"]
    assert len(table["rows"]) == 7


def test_table_with_header_row_and_nested_table(parser):
    html = """<table><thead><tr><th>Name</th><th>Qty</th></tr></thead>
    <tbody><tr><td>apple</td><td>3</td></tr>
    <tr><td>box</td><td><table><tr><td>inner</td></tr></table></td></tr></tbody></table>
    <table></table>"""
    outer, inner = parser.extract_tables(html)
    assert outer["headers"] == ["Name", "Qty"]
    assert outer["rows"] == [["apple", "3"], ["box", "inner"]]
    assert inner == {"headers": [], "rows": [["inner"]]}


def test_lists(parser):
    html = "<ul><li>a</li><li> b </li><li></li></ul><ol><li>one<ul><li>x</li></ul></li></ol><ul></ul>"
    assert parser.extract_lists(html) == [
        {"type": "ul", "items": ["a", "b"]},
        {"type": "ol", "items": ["one x"]},
        {"type": "ul", "items": ["x"]},
    ]
    assert parser.extract_lists(load("books_product.html")) == [
        {"type": "ul", "items": ["Home", "Books", "Poetry", "A Light in the Attic"]},
    ]


# --- parse (full page breakdown) ---------------------------------------------

async def test_parse_returns_all_sections(parser):
    page = await parser.parse(load("books_product.html"), PRODUCT_URL)
    assert set(page) == {"url", "title", "text", "links", "metadata", "images", "headings", "tables", "lists"}
    assert page["url"] == PRODUCT_URL
    assert page["title"] == page["metadata"]["title"] == "A Light in the Attic | Books to Scrape - Sandbox"
    assert page["links"] == parser.extract_links(load("books_product.html"), PRODUCT_URL)
    assert page["headings"]["h1"] == ["A Light in the Attic"]
    assert len(page["tables"]) == 1 and len(page["images"]) == 1


async def test_parse_empty_page(parser):
    page = await parser.parse("", "http://site.test/")
    assert page["title"] is None and page["text"] == "" and page["links"] == []
    assert page["images"] == [] and page["tables"] == [] and page["lists"] == []
