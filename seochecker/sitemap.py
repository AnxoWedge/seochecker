"""Sitemap discovery and parsing.

Handles the three shapes that turn up in the wild: `<urlset>`, `<sitemapindex>`
(recursed), and the plain-text one-URL-per-line form the spec also permits.
Namespaces are matched by local name, because plenty of sitemaps declare the
wrong one or none at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from xml.etree import ElementTree

if TYPE_CHECKING:
    from .fetch import Fetcher

# Spec limits. A sitemap larger than this is malformed, and we stop rather than
# let one bad file define the crawl.
MAX_URLS_PER_SITEMAP = 50_000
MAX_SITEMAP_FILES = 50
MAX_INDEX_DEPTH = 3

_XML_DECLARATION = re.compile(r"^\s*<\?xml[^>]*\?>", re.I)


@dataclass(slots=True)
class SitemapEntry:
    loc: str
    lastmod: str = ""
    changefreq: str = ""
    priority: str = ""
    source: str = ""


@dataclass(slots=True)
class SitemapSet:
    entries: list[SitemapEntry] = field(default_factory=list)
    fetched: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    truncated: bool = False

    @property
    def urls(self) -> list[str]:
        return [entry.loc for entry in self.entries]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _text(node: ElementTree.Element, name: str) -> str:
    for child in node:
        if _local(child.tag) == name:
            return (child.text or "").strip()
    return ""


def parse_sitemap(text: str, source_url: str = "") -> tuple[str, list[SitemapEntry], list[str]]:
    """Return (kind, url entries, child sitemap URLs).

    `kind` is one of `urlset`, `sitemapindex`, `text` or `unknown`.
    """
    stripped = text.strip()
    if not stripped:
        return "unknown", [], []

    if not stripped.startswith("<"):
        # The plain-text form: one absolute URL per line.
        entries = [
            SitemapEntry(loc=line.strip(), source=source_url)
            for line in stripped.splitlines()
            if line.strip().lower().startswith(("http://", "https://"))
        ]
        return ("text", entries, []) if entries else ("unknown", [], [])

    try:
        root = ElementTree.fromstring(_XML_DECLARATION.sub("", stripped))
    except ElementTree.ParseError:
        return "unknown", [], []

    kind = _local(root.tag)
    if kind == "sitemapindex":
        children = [loc for node in root
                    if _local(node.tag) == "sitemap" and (loc := _text(node, "loc"))]
        return "sitemapindex", [], children

    if kind == "urlset":
        entries = []
        for node in root:
            if _local(node.tag) != "url":
                continue
            if loc := _text(node, "loc"):
                entries.append(
                    SitemapEntry(
                        loc=loc,
                        lastmod=_text(node, "lastmod"),
                        changefreq=_text(node, "changefreq"),
                        priority=_text(node, "priority"),
                        source=source_url,
                    )
                )
        return "urlset", entries, []

    return "unknown", [], []


async def load_sitemaps(
    fetcher: "Fetcher",
    seeds: list[str],
    *,
    max_urls: int = MAX_URLS_PER_SITEMAP,
    max_files: int = MAX_SITEMAP_FILES,
    max_depth: int = MAX_INDEX_DEPTH,
) -> SitemapSet:
    """Fetch the seed sitemaps, expanding index files breadth-first."""
    result = SitemapSet()
    seen: set[str] = set()
    queue: list[tuple[str, int]] = [(url, 0) for url in seeds]

    while queue:
        url, depth = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)

        if len(result.fetched) >= max_files:
            result.truncated = True
            break

        page = await fetcher.fetch(url, force_read=True)
        if page.error is not None:
            result.failed.append((url, page.error.value))
            continue
        if not page.ok:
            result.failed.append((url, f"HTTP {page.status}"))
            continue
        if not page.html:
            result.failed.append((url, "empty body"))
            continue

        kind, entries, children = parse_sitemap(page.html, source_url=page.final_url)
        if kind == "unknown":
            result.failed.append((url, "not a recognisable sitemap"))
            continue

        result.fetched.append(page.final_url)

        if children:
            if depth >= max_depth:
                result.truncated = True
            else:
                queue.extend((child, depth + 1) for child in children)

        room = max_urls - len(result.entries)
        if len(entries) > room:
            result.truncated = True
            entries = entries[:room]
        result.entries.extend(entries)
        if len(result.entries) >= max_urls:
            result.truncated = True
            break

    return result
