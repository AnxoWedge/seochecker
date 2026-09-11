"""The internal link graph.

Three questions this answers that nothing else can:

- **Where does authority pool?** Internal PageRank over the site's own links. No
  third-party API can tell you this, and it is usually what people are really
  asking when they ask about authority.
- **What is unreachable?** Orphans — in the sitemap, or crawled, but with no
  internal link pointing at them.
- **How far is everything from the front door?** Click depth measured over the
  link graph, not the crawl order, which sitemap seeding distorts.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field

from .models import Page
from .urls import normalize

DAMPING = 0.85
MAX_ITERATIONS = 60
CONVERGENCE = 1e-6


@dataclass
class LinkGraph:
    nodes: list[str] = field(default_factory=list)
    outgoing: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    incoming: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    pagerank: dict[str, float] = field(default_factory=dict)
    click_depth: dict[str, int] = field(default_factory=dict)
    root: str = ""

    # --- construction ------------------------------------------------------

    @classmethod
    def build(cls, pages: list[Page], root: str) -> "LinkGraph":
        graph = cls(root=normalize(root))
        known = {normalize(page.requested_url) for page in pages}
        graph.nodes = sorted(known)

        for page in pages:
            source = normalize(page.requested_url)
            for target in page.outlinks:
                # Only edges between pages we actually fetched; a link to a URL
                # the budget never reached says nothing about its authority.
                if target in known and target != source:
                    graph.outgoing[source].add(target)
                    graph.incoming[target].add(source)

        graph._compute_pagerank()
        graph._compute_click_depth()
        return graph

    # --- metrics -----------------------------------------------------------

    def _compute_pagerank(self) -> None:
        nodes = self.nodes
        count = len(nodes)
        if not count:
            return

        rank = {node: 1.0 / count for node in nodes}
        index = {node: i for i, node in enumerate(nodes)}
        out_degree = {node: len(self.outgoing.get(node, ())) for node in nodes}

        for _ in range(MAX_ITERATIONS):
            # Pages with no outgoing links would leak rank out of the system, so
            # their share is redistributed evenly — the standard treatment.
            dangling = sum(rank[node] for node in nodes if not out_degree[node])
            updated = {}
            base = (1.0 - DAMPING) / count + DAMPING * dangling / count
            for node in nodes:
                inbound = sum(
                    rank[source] / out_degree[source]
                    for source in self.incoming.get(node, ())
                    if out_degree[source]
                )
                updated[node] = base + DAMPING * inbound

            delta = sum(abs(updated[node] - rank[node]) for node in nodes)
            rank = updated
            if delta < CONVERGENCE:
                break

        self.pagerank = rank

    def _compute_click_depth(self) -> None:
        """Breadth-first from the root over internal links. Unreachable stays unset."""
        if self.root not in self.pagerank and self.root not in self.outgoing:
            # The crawl may have redirected; fall back to the shallowest node.
            candidates = [n for n in self.nodes if n.count("/") <= 3]
            if not candidates:
                return
            self.root = min(candidates, key=len)

        depth = {self.root: 0}
        queue = deque([self.root])
        while queue:
            node = queue.popleft()
            for target in self.outgoing.get(node, ()):
                if target not in depth:
                    depth[target] = depth[node] + 1
                    queue.append(target)
        self.click_depth = depth

    # --- questions ---------------------------------------------------------

    def orphans(self) -> list[str]:
        """Crawled pages that nothing on the site links to."""
        return [node for node in self.nodes
                if node != self.root and not self.incoming.get(node)]

    def unreachable(self) -> list[str]:
        """Pages no chain of internal links reaches from the root."""
        return [node for node in self.nodes if node not in self.click_depth]

    def dead_ends(self) -> list[str]:
        return [node for node in self.nodes if not self.outgoing.get(node)]

    def top_by_pagerank(self, limit: int = 10) -> list[tuple[str, float]]:
        return sorted(self.pagerank.items(), key=lambda item: -item[1])[:limit]

    def apply_to(self, pages: list[Page]) -> None:
        """Write the computed metrics back onto the pages."""
        for page in pages:
            node = normalize(page.requested_url)
            page.pagerank = round(self.pagerank.get(node, 0.0), 6)
            page.click_depth = self.click_depth.get(node)
            page.inlink_count = len(self.incoming.get(node, ()))

    def summary(self) -> dict[str, object]:
        edges = sum(len(targets) for targets in self.outgoing.values())
        return {
            "nodes": len(self.nodes),
            "edges": edges,
            "orphans": len(self.orphans()),
            "unreachable": len(self.unreachable()),
            "dead_ends": len(self.dead_ends()),
            "max_click_depth": max(self.click_depth.values(), default=0),
            "top_by_pagerank": [
                {"url": url, "pagerank": round(score, 6)}
                for url, score in self.top_by_pagerank()
            ],
        }
