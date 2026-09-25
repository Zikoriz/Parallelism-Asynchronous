import asyncio
from urllib.parse import urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup
from bs4.element import PreformattedString

ALLOWED_SCHEMES = ("http", "https")

# field name -> CSS selector (str) or {"selector": ..., "attr": ..., "all": bool}
DEFAULT_SELECTORS: dict[str, str | dict] = {
    "title": "title",
    "description": 'meta[name="description"]',
}

# tags whose text is never visible page content. <head> itself is not listed: with an
# unclosed <head> html.parser nests the whole body inside it (see tests/fixtures/broken.html)
NON_CONTENT_TAGS = frozenset({"script", "style", "noscript", "template", "title", "svg"})
HEADING_TAGS = ("h1", "h2", "h3")

Markup = str | BeautifulSoup | None


class HTMLParser:
    """Parses HTML with BeautifulSoup and extracts links, text, metadata and structured content.

    Every extractor accepts either a ready BeautifulSoup (from parse_html) or a raw HTML
    string, so a page can be parsed once and reused. Tolerant to broken markup and empty pages.
    """

    def __init__(self, features: str = "html.parser"):
        self.features = features

    async def parse_html(self, html: str | None) -> BeautifulSoup:
        """Build the soup in a worker thread: parsing is CPU-bound and would block the event loop."""
        return await asyncio.to_thread(self._soup, html)

    def _soup(self, markup: Markup) -> BeautifulSoup:
        if isinstance(markup, BeautifulSoup):
            return markup
        return BeautifulSoup(markup or "", self.features)

    @staticmethod
    def _base_url(soup: BeautifulSoup, base_url: str) -> str:
        base_tag = soup.find("base", href=True)
        return urljoin(base_url, base_tag["href"].strip()) if base_tag else base_url

    def extract_links(self, soup: Markup, base_url: str) -> list[str]:
        """Absolute http(s) links in document order, without fragments and duplicates."""
        soup = self._soup(soup)
        base_url = self._base_url(soup, base_url)

        links: list[str] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith("#"):
                continue
            url, _ = urldefrag(urljoin(base_url, href))
            if urlsplit(url).scheme.lower() not in ALLOWED_SCHEMES:
                continue  # mailto:, javascript:, tel:, data:, ...
            if url not in seen:
                seen.add(url)
                links.append(url)
        return links

    def extract_text(self, soup: Markup) -> str:
        """Visible text of the page: scripts, styles, <title>, comments and doctype skipped, whitespace collapsed."""
        soup = self._soup(soup)
        parts = [
            s for s in soup.find_all(string=True)
            if not isinstance(s, PreformattedString)  # comments, doctype, CDATA
            and not any(p.name in NON_CONTENT_TAGS for p in s.parents)
        ]
        return " ".join(" ".join(parts).split())

    def extract_metadata(self, soup: Markup) -> dict:
        """title, description, keywords (list), canonical URL (as written) and document language."""
        soup = self._soup(soup)
        keywords = self._meta(soup, "keywords")
        canonical = soup.find("link", rel="canonical", href=True)
        html_tag = soup.find("html")
        return {
            "title": _text(soup.title) if soup.title else None,
            "description": self._meta(soup, "description"),
            "keywords": [k.strip() for k in keywords.split(",") if k.strip()] if keywords else [],
            "canonical": canonical["href"].strip() if canonical else None,
            "language": html_tag.get("lang") if html_tag else None,
        }

    @staticmethod
    def _meta(soup: BeautifulSoup, name: str) -> str | None:
        # the name attribute is case-insensitive in practice: "Description", "KEYWORDS"
        tag = soup.find("meta", attrs={"name": lambda v: v is not None and v.lower() == name})
        content = tag.get("content") if tag else None
        return content.strip() if content is not None else None

    def extract_images(self, soup: Markup, base_url: str) -> list[dict]:
        """[{"src": absolute URL, "alt": str | None}] for every <img> with a non-empty src."""
        soup = self._soup(soup)
        base_url = self._base_url(soup, base_url)
        images = []
        for img in soup.find_all("img", src=True):
            src = img["src"].strip()
            if src:
                alt = img.get("alt")
                images.append({"src": urljoin(base_url, src), "alt": alt.strip() if alt is not None else None})
        return images

    def extract_headings(self, soup: Markup) -> dict[str, list[str]]:
        """{"h1": [...], "h2": [...], "h3": [...]} in document order; empty headings dropped."""
        soup = self._soup(soup)
        headings: dict[str, list[str]] = {tag: [] for tag in HEADING_TAGS}
        for h in soup.find_all(HEADING_TAGS):
            if text := _text(h):
                headings[h.name].append(text)
        return headings

    def extract_tables(self, soup: Markup) -> list[dict]:
        """[{"headers": [...], "rows": [[...], ...]}]; the first row is the header if it is all <th>."""
        soup = self._soup(soup)
        tables = []
        for table in soup.find_all("table"):
            rows, headers = [], []
            for tr in table.find_all("tr"):
                if tr.find_parent("table") is not table:
                    continue  # row of a nested table: reported with that table
                cells = tr.find_all(["th", "td"], recursive=False)
                if not cells:
                    continue
                if not rows and not headers and all(c.name == "th" for c in cells):
                    headers = [_text(c) for c in cells]
                else:
                    rows.append([_text(c) for c in cells])
            if headers or rows:
                tables.append({"headers": headers, "rows": rows})
        return tables

    def extract_lists(self, soup: Markup) -> list[dict]:
        """[{"type": "ul" | "ol", "items": [...]}] from direct <li> children; empty lists dropped."""
        soup = self._soup(soup)
        lists = []
        for lst in soup.find_all(["ul", "ol"]):
            items = [text for li in lst.find_all("li", recursive=False) if (text := _text(li))]
            if items:
                lists.append({"type": lst.name, "items": items})
        return lists

    async def parse(self, html: str | None, url: str) -> dict:
        """Full page breakdown: url, title, text, links, metadata, images, headings, tables, lists."""
        soup = await self.parse_html(html)
        metadata = self.extract_metadata(soup)
        return {
            "url": url,
            "title": metadata["title"],
            "text": self.extract_text(soup),
            "links": self.extract_links(soup, url),
            "metadata": metadata,
            "images": self.extract_images(soup, url),
            "headings": self.extract_headings(soup),
            "tables": self.extract_tables(soup),
            "lists": self.extract_lists(soup),
        }

    def extract_data(self, soup: Markup, selectors: dict[str, str | dict] | None = None) -> dict:
        """Extract fields by CSS selectors; a missing element yields None, never an exception.

        A selector spec is either a CSS string or a dict:
          selector — CSS selector (required)
          attr     — attribute to read instead of text (meta tags default to "content")
          all      — return a list of every match instead of the first one
        """
        soup = self._soup(soup)
        data: dict = {}
        for name, spec in (selectors if selectors is not None else DEFAULT_SELECTORS).items():
            if isinstance(spec, str):
                spec = {"selector": spec}
            attr = spec.get("attr")
            if spec.get("all"):
                data[name] = [self._value(el, attr) for el in soup.select(spec["selector"])]
            else:
                el = soup.select_one(spec["selector"])
                data[name] = None if el is None else self._value(el, attr)
        return data

    @staticmethod
    def _value(el, attr: str | None) -> str | None:
        if attr is None and el.name == "meta":
            attr = "content"
        if attr is not None:
            value = el.get(attr)
            if isinstance(value, list):  # multi-valued attributes such as class
                value = " ".join(value)
            return value.strip() if value is not None else None
        return _text(el)


def _text(el) -> str:
    return " ".join(el.get_text(" ").split())  # collapse newlines/indentation inside text
