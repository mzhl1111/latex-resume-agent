"""Small stdlib HTML -> Markdown-ish text converter for job descriptions."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_SKIP = {"script", "style", "noscript", "svg", "template", "head", "iframe", "form", "button"}
_CHROME = {"nav", "header", "footer", "aside"}
_BLOCK = {
    "p", "div", "section", "article", "main", "br", "tr", "table", "ul", "ol",
    "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "dd", "dt",
}


class _Converter(HTMLParser):
    def __init__(self, drop_chrome: bool):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip_depth = 0
        self.drop_chrome = drop_chrome

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP or (self.drop_chrome and tag in _CHROME):
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.out.append("\n\n" + "#" * min(int(tag[1]) + 1, 4) + " ")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in {"strong", "b"}:
            self.out.append("**")
        elif tag in _BLOCK:
            self.out.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag == "br" and not self.skip_depth:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP or (self.drop_chrome and tag in _CHROME):
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag in {"strong", "b"}:
            self.out.append("**")
        elif tag in _BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip_depth:
            self.out.append(re.sub(r"\s+", " ", data))


def html_to_text(markup: str, *, drop_chrome: bool = True) -> str:
    """Convert an HTML fragment to readable text with headings and bullets kept."""
    if "&lt;" in markup and "<" not in markup.replace("&lt;", ""):
        markup = html.unescape(markup)  # Greenhouse double-escapes its content
    conv = _Converter(drop_chrome)
    conv.feed(markup)
    conv.close()
    text = "".join(conv.out)
    text = text.replace("****", "")
    lines = [ln.strip() for ln in text.splitlines()]
    cleaned: list[str] = []
    for ln in lines:
        if ln in {"-", "**", "#", "##", "###", "####"}:
            continue
        if not ln and cleaned and not cleaned[-1]:
            continue
        cleaned.append(ln)
    return "\n".join(cleaned).strip()
