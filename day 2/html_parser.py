from urllib.parse import urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup

ALLOWED_SCHEMES = ("http", "https")

# field name -> CSS selector (str) or {"selector": ..., "attr": ..., "all": bool}
DEFAULT_SELECTORS: dict[str, str | dict] = {
    "title": "title",
    "description": 'meta[name="description"]',
}


class HTMLParser:
    """Extracts links and configurable fields from HTML; tolerant to broken markup."""

    def __init__(self, features: str = "html.parser"):
        self.features = features

    def _soup(self, html: str | None) -> BeautifulSoup:
        return BeautifulSoup(html or "", self.features)

    def extract_links(self, html: str, base_url: str) -> list[str]:
        """Absolute http(s) links in document order, without fragments and duplicates."""
        soup = self._soup(html)
        base_tag = soup.find("base", href=True)
        if base_tag:
            base_url = urljoin(base_url, base_tag["href"].strip())

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

    def extract_data(self, html: str, selectors: dict[str, str | dict] | None = None) -> dict:
        """Extract fields by CSS selectors; a missing element yields None, never an exception.

        A selector spec is either a CSS string or a dict:
          selector — CSS selector (required)
          attr     — attribute to read instead of text (meta tags default to "content")
          all      — return a list of every match instead of the first one
        """
        soup = self._soup(html)
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
        return " ".join(el.get_text(" ").split())  # collapse newlines/indentation inside text
