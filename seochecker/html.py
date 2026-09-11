"""A thin, analyzer-friendly wrapper around the parsed DOM.

Every Phase 2 analyzer takes a `Document`, so the parser stays swappable and the
fiddly parts — `<base href>`, resolving relative URLs, skipping `javascript:`
links, not counting `<script>` text as content — are solved once, here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Iterator
from urllib.parse import urljoin, urlsplit

try:  # lexbor is the faster, more spec-compliant backend
    from selectolax.lexbor import LexborHTMLParser as _Parser
except ImportError:  # pragma: no cover
    from selectolax.parser import HTMLParser as _Parser

NON_NAVIGATIONAL = ("javascript:", "mailto:", "tel:", "data:", "sms:", "callto:")
_WORD = re.compile(r"\w", re.UNICODE)
_WS = re.compile(r"\s+")


@dataclass(slots=True)
class Link:
    href: str            # exactly as authored
    url: str             # resolved against the base, fragment stripped
    text: str
    rel: list[str] = field(default_factory=list)
    target: str = ""
    internal: bool = False
    image_alt: str | None = None   # None = the link contains no <img>

    @property
    def anchor(self) -> str:
        """Effective anchor text: visible text, else the wrapped image's alt."""
        return self.text or (self.image_alt or "")

    @property
    def nofollow(self) -> bool:
        return "nofollow" in self.rel


@dataclass(slots=True)
class Script:
    src: str
    url: str
    type: str = ""
    is_async: bool = False
    defer: bool = False
    in_head: bool = False
    inline_length: int = 0

    @property
    def render_blocking(self) -> bool:
        """A `<head>` script with a src and no async/defer stalls first paint.

        `type="module"` is deferred by default, so it does not.
        """
        return bool(
            self.src and self.in_head and not self.is_async
            and not self.defer and self.type != "module"
        )


@dataclass(slots=True)
class Image:
    src: str
    url: str
    alt: str | None       # None = attribute absent, "" = present but empty
    title: str = ""
    width: str = ""
    height: str = ""
    loading: str = ""
    srcset: str = ""

    @property
    def has_alt(self) -> bool:
        return self.alt is not None

    @property
    def decorative(self) -> bool:
        return self.alt == ""


class Document:
    """Parsed HTML for one page."""

    def __init__(self, html: str, url: str) -> None:
        self.url = url
        self.raw = html
        self.tree = _Parser(html)

    # --- basics ------------------------------------------------------------

    def _attr(self, selector: str, attribute: str) -> str:
        node = self.tree.css_first(selector)
        if node is None:
            return ""
        return (node.attributes.get(attribute) or "").strip()

    def _text_of(self, selector: str) -> str:
        node = self.tree.css_first(selector)
        return _WS.sub(" ", node.text()).strip() if node is not None else ""

    @cached_property
    def base_url(self) -> str:
        """`<base href>` wins over the document URL for relative resolution."""
        href = self._attr("base[href]", "href")
        return urljoin(self.url, href) if href else self.url

    def absolute(self, href: str) -> str:
        try:
            resolved = urljoin(self.base_url, href.strip())
        except ValueError:
            return ""
        return urlsplit(resolved)._replace(fragment="").geturl()

    def is_internal(self, url: str) -> bool:
        return urlsplit(url).netloc.lower() == urlsplit(self.url).netloc.lower()

    # --- head --------------------------------------------------------------

    @cached_property
    def titles(self) -> list[str]:
        """Page titles — more than one `<title>` is itself a finding.

        Inline SVG icons carry their own `<title>` for accessibility, and a page
        with 25 icons is not a page with 25 titles, so those are filtered out.
        """
        return [
            _WS.sub(" ", node.text()).strip()
            for node in self.tree.css("title")
            if not _inside(node, ("svg", "math"))
        ]

    @property
    def title(self) -> str:
        return self.titles[0] if self.titles else ""

    def meta(self, name: str) -> str:
        for node in self.tree.css("meta[name]"):
            if (node.attributes.get("name") or "").strip().lower() == name.lower():
                return (node.attributes.get("content") or "").strip()
        return ""

    def meta_property(self, prop: str) -> str:
        for node in self.tree.css("meta[property]"):
            if (node.attributes.get("property") or "").strip().lower() == prop.lower():
                return (node.attributes.get("content") or "").strip()
        return ""

    @property
    def description(self) -> str:
        return self.meta("description")

    @property
    def robots(self) -> str:
        return self.meta("robots")

    @property
    def viewport(self) -> str:
        return self.meta("viewport")

    @property
    def generator(self) -> str:
        return self.meta("generator")

    @cached_property
    def link_tags(self) -> list[tuple[list[str], dict[str, str]]]:
        """Every `<link>` as (rel tokens, attributes). One pass, many consumers."""
        out: list[tuple[list[str], dict[str, str]]] = []
        for node in self.tree.css("link"):
            attrs = {k: (v or "") for k, v in node.attributes.items()}
            out.append((attrs.get("rel", "").lower().split(), attrs))
        return out

    def links_with_rel(self, rel: str) -> list[dict[str, str]]:
        return [attrs for rels, attrs in self.link_tags if rel in rels]

    @cached_property
    def canonicals(self) -> list[str]:
        """All of them — conflicting canonicals are a finding, so keep the list."""
        return [
            self.absolute(attrs["href"])
            for attrs in self.links_with_rel("canonical")
            if attrs.get("href", "").strip()
        ]

    @property
    def canonical(self) -> str:
        return self.canonicals[0] if self.canonicals else ""

    @cached_property
    def canonical_raw(self) -> str:
        for attrs in self.links_with_rel("canonical"):
            if href := attrs.get("href", "").strip():
                return href
        return ""

    @cached_property
    def hreflangs(self) -> list[tuple[str, str, str]]:
        """(hreflang value, raw href, resolved url) for every alternate."""
        out: list[tuple[str, str, str]] = []
        for rels, attrs in self.link_tags:
            if "alternate" not in rels:
                continue
            lang = attrs.get("hreflang", "").strip()
            href = attrs.get("href", "").strip()
            if lang and href:
                out.append((lang, href, self.absolute(href)))
        return out

    @cached_property
    def stylesheets(self) -> list[str]:
        return [
            self.absolute(attrs["href"])
            for attrs in self.links_with_rel("stylesheet")
            if attrs.get("href", "").strip()
        ]

    @cached_property
    def favicons(self) -> list[str]:
        out: list[str] = []
        for rels, attrs in self.link_tags:
            if any(r in rels for r in ("icon", "shortcut", "apple-touch-icon", "mask-icon")):
                if href := attrs.get("href", "").strip():
                    out.append(self.absolute(href))
        return out

    @property
    def lang(self) -> str:
        return self._attr("html", "lang")

    @property
    def charset_meta(self) -> str:
        return self._attr("meta[charset]", "charset").lower()

    # --- body --------------------------------------------------------------

    @cached_property
    def metas(self) -> list[tuple[str, str]]:
        """(lowercased name, content) for every `<meta name=...>`."""
        out: list[tuple[str, str]] = []
        for node in self.tree.css("meta[name]"):
            name = (node.attributes.get("name") or "").strip().lower()
            if name:
                out.append((name, (node.attributes.get("content") or "").strip()))
        return out

    def meta_all(self, name: str) -> list[str]:
        return [content for key, content in self.metas if key == name.lower()]

    @cached_property
    def scripts(self) -> list["Script"]:
        out: list[Script] = []
        for node in self.tree.css("script"):
            attrs = node.attributes
            src = (attrs.get("src") or "").strip()
            out.append(
                Script(
                    src=src,
                    url=self.absolute(src) if src else "",
                    type=(attrs.get("type") or "").strip().lower(),
                    is_async="async" in attrs,
                    defer="defer" in attrs,
                    in_head=_inside(node, ("head",)),
                    inline_length=0 if src else len(node.text() or ""),
                )
            )
        return out

    @cached_property
    def subresources(self) -> list[tuple[str, str]]:
        """(tag, resolved url) for everything the browser will also request."""
        out: list[tuple[str, str]] = []
        selector = ("img[src], script[src], iframe[src], video[src], audio[src], "
                    "source[src], embed[src], track[src], object[data]")
        for node in self.tree.css(selector):
            attrs = node.attributes
            value = (attrs.get("src") or attrs.get("data") or "").strip()
            if value and (url := self.absolute(value)):
                out.append((node.tag, url))
        for rels, attrs in self.link_tags:
            if any(r in rels for r in ("stylesheet", "preload", "icon", "manifest",
                                       "apple-touch-icon", "preconnect", "prefetch")):
                if href := attrs.get("href", "").strip():
                    if url := self.absolute(href):
                        out.append(("link", url))
        for image in self.images:
            for candidate in image.srcset.split(","):
                candidate = candidate.strip().split(" ")[0]
                if candidate and (url := self.absolute(candidate)):
                    out.append(("srcset", url))
        return out

    @property
    def has_noscript(self) -> bool:
        return self.tree.css_first("noscript") is not None

    @cached_property
    def headings(self) -> list[tuple[int, str]]:
        """(level, text) in document order."""
        out: list[tuple[int, str]] = []
        for node in self.tree.css("h1, h2, h3, h4, h5, h6"):
            out.append((int(node.tag[1]), _WS.sub(" ", node.text()).strip()))
        return out

    @property
    def h1s(self) -> list[str]:
        return [text for level, text in self.headings if level == 1]

    @cached_property
    def links(self) -> list[Link]:
        out: list[Link] = []
        for node in self.tree.css("a[href]"):
            href = (node.attributes.get("href") or "").strip()
            if not href or href.startswith("#"):
                continue
            if href.lower().startswith(NON_NAVIGATIONAL):
                continue
            url = self.absolute(href)
            if not url:
                continue
            rel = (node.attributes.get("rel") or "").lower().split()
            image = node.css_first("img")
            out.append(
                Link(
                    image_alt=(image.attributes.get("alt") or "") if image is not None else None,
                    href=href,
                    url=url,
                    text=_WS.sub(" ", node.text()).strip(),
                    rel=rel,
                    target=(node.attributes.get("target") or "").strip(),
                    internal=self.is_internal(url),
                )
            )
        return out

    @cached_property
    def images(self) -> list[Image]:
        out: list[Image] = []
        for node in self.tree.css("img"):
            attrs = node.attributes
            src = (attrs.get("src") or attrs.get("data-src") or "").strip()
            out.append(
                Image(
                    src=src,
                    url=self.absolute(src) if src else "",
                    alt=attrs.get("alt"),   # deliberately not defaulted
                    title=(attrs.get("title") or "").strip(),
                    width=(attrs.get("width") or "").strip(),
                    height=(attrs.get("height") or "").strip(),
                    loading=(attrs.get("loading") or "").strip().lower(),
                    srcset=(attrs.get("srcset") or "").strip(),
                )
            )
        return out

    @cached_property
    def json_ld(self) -> list[Any]:
        """Parsed JSON-LD blocks. Malformed blocks are reported, not raised."""
        blocks: list[Any] = []
        for node in self.tree.css('script[type="application/ld+json"]'):
            payload = node.text().strip()
            if not payload:
                continue
            try:
                blocks.append(json.loads(payload))
            except json.JSONDecodeError as exc:
                blocks.append({"__parse_error__": str(exc)})
        return blocks

    @cached_property
    def lower_raw(self) -> str:
        """Lowercased source, for case-insensitive substring pre-filtering."""
        return self.raw.lower()

    @cached_property
    def text(self) -> str:
        """Visible text, with script/style/noscript removed.

        Parsed a second time so the main tree stays intact for other analyzers.
        """
        tree = _Parser(self.raw)
        tree.strip_tags(["script", "style", "noscript", "template", "svg"])
        body = tree.body or tree.root
        return _WS.sub(" ", body.text(separator=" ")).strip() if body else ""

    @cached_property
    def word_count(self) -> int:
        return sum(1 for token in self.text.split() if _WORD.search(token))

    # --- phase 1 output ----------------------------------------------------

    def summary(self) -> dict[str, Any]:
        """The at-a-glance shape. Phase 2 replaces this with real findings."""
        missing_alt = [img for img in self.images if not img.has_alt]
        return {
            "title": self.title,
            "title_length": len(self.title),
            "title_count": len(self.titles),
            "description": self.description,
            "description_length": len(self.description),
            "canonical": self.canonical,
            "robots_meta": self.robots,
            "lang": self.lang,
            "hreflang": [[lang, url] for lang, _raw, url in self.hreflangs],
            "viewport": self.viewport,
            "charset_meta": self.charset_meta,
            "generator": self.generator,
            "og_title": self.meta_property("og:title"),
            "og_description": self.meta_property("og:description"),
            "og_image": self.absolute(self.meta_property("og:image")) or "",
            "twitter_card": self.meta("twitter:card"),
            "h1": self.h1s,
            "h1_count": len(self.h1s),
            "heading_counts": {
                f"h{level}": sum(1 for lvl, _ in self.headings if lvl == level)
                for level in range(1, 7)
            },
            "word_count": self.word_count,
            "links_total": len(self.links),
            "links_internal": sum(1 for link in self.links if link.internal),
            "links_external": sum(1 for link in self.links if not link.internal),
            "links_nofollow": sum(1 for link in self.links if link.nofollow),
            "images_total": len(self.images),
            "images_missing_alt": len(missing_alt),
            "images_missing_alt_src": [img.src for img in missing_alt[:10]],
            "images_decorative": sum(1 for img in self.images if img.decorative),
            "json_ld_blocks": len(self.json_ld),
            "json_ld_types": sorted(_schema_types(self.json_ld)),
        }


def _inside(node: Any, tags: tuple[str, ...]) -> bool:
    """True if any ancestor of `node` is one of `tags`."""
    parent = node.parent
    while parent is not None:
        if parent.tag in tags:
            return True
        parent = parent.parent
    return False


def _schema_types(blocks: list[Any]) -> set[str]:
    """Pull @type out of JSON-LD, including @graph and nested arrays."""
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            value = node.get("@type")
            if isinstance(value, str):
                found.add(value)
            elif isinstance(value, list):
                found.update(str(v) for v in value)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(blocks)
    return found
