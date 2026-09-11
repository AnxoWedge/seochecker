"""Grouping pages into hreflang clusters.

Google's rule is specific: "Localized versions of a page are only considered
duplicates if the main content of the page remains untranslated." So a set of
pages that declare each other as language alternates is not a duplicate problem
when the content really is translated — and *is* one when it is not.

That distinction is what this module exists to draw. It also follows Google on
reciprocity: "If two pages don't both point to each other, the tags will be
ignored", so a one-way declaration does not form a cluster between two pages we
have both seen.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import Page
from .urls import normalize


@dataclass
class LanguageClusters:
    """Which crawled pages are declared language alternates of which."""

    cluster_of: dict[str, int] = field(default_factory=dict)     # url -> cluster id
    members: dict[int, set[str]] = field(default_factory=dict)   # cluster id -> urls
    languages: dict[str, str] = field(default_factory=dict)      # url -> hreflang value

    def same_cluster(self, left: str, right: str) -> bool:
        a = self.cluster_of.get(normalize(left))
        b = self.cluster_of.get(normalize(right))
        return a is not None and a == b

    def cluster_for(self, url: str) -> int | None:
        return self.cluster_of.get(normalize(url))

    def collapse(self, pages: list[Page]) -> list[Page]:
        """One representative per cluster, plus every page in no cluster.

        Used to ask "is this still a duplicate once language variants are
        accounted for?" — if the collapsed list has one entry, it is not.
        """
        seen: set[int] = set()
        out: list[Page] = []
        for page in pages:
            cluster = self.cluster_for(page.final_url)
            if cluster is None:
                out.append(page)
                continue
            if cluster not in seen:
                seen.add(cluster)
                out.append(page)
        return out

    def describe(self, pages: list[Page]) -> str:
        parts = []
        for page in pages:
            lang = self.languages.get(normalize(page.final_url)) or (page.seo or {}).get("lang")
            parts.append(f"{page.final_url} ({lang})" if lang else page.final_url)
        return ", ".join(parts)


def build(pages: list[Page]) -> LanguageClusters:
    """Group crawled pages by their declared hreflang relationships."""
    clusters = LanguageClusters()

    known = {normalize(page.requested_url): page for page in pages}
    for page in pages:
        known.setdefault(normalize(page.final_url), page)

    # Who does each page declare as an alternate, and under what language?
    declared: dict[str, set[str]] = {}
    for page in pages:
        url = normalize(page.final_url)
        alternates = (page.seo or {}).get("hreflang") or []
        targets: set[str] = set()
        for entry in alternates:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                continue
            lang, target = entry
            target_url = normalize(target)
            if str(lang).lower() == "x-default":
                continue
            if target_url == url:
                clusters.languages[url] = str(lang)
                continue
            targets.add(target_url)
            clusters.languages.setdefault(target_url, str(lang))
        if targets:
            declared[url] = targets

    # An edge counts when both pages point at each other, or when the other page
    # was never crawled so we cannot know that it doesn't.
    def linked(left: str, right: str) -> bool:
        forward = right in declared.get(left, set())
        if not forward:
            return False
        if right not in known:
            return True
        return left in declared.get(right, set())

    # Union-find over those edges.
    parent: dict[str, str] = {}

    def find(node: str) -> str:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: str, b: str) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_b] = root_a

    for left, targets in declared.items():
        for right in targets:
            if linked(left, right):
                union(left, right)

    groups: dict[str, set[str]] = {}
    for node in list(parent):
        groups.setdefault(find(node), set()).add(node)

    for index, (_root, urls) in enumerate(sorted(groups.items())):
        if len(urls) < 2:
            continue
        clusters.members[index] = urls
        for url in urls:
            clusters.cluster_of[url] = index
    return clusters
