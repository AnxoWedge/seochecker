"""Site structure: reachability, link health, and where authority actually sits."""

from __future__ import annotations

from collections import defaultdict
from typing import Iterator
from urllib.parse import urlsplit

from ..models import Finding, Page
from ..similarity import near_duplicate
from ..urls import normalize
from .base import SiteContext, critical, info, notice, sample, site_analyzer, warning

MAX_GOOD_CLICK_DEPTH = 3


@site_analyzer
def reachability(ctx: SiteContext) -> Iterator[Finding]:
    graph = ctx.graph
    if not graph.nodes:
        return

    by_url = {normalize(p.requested_url): p for p in ctx.pages}

    if orphans := [url for url in graph.orphans() if by_url.get(url, Page("")).ok]:
        from_sitemap = [url for url in orphans
                        if by_url[url].from_sitemap or url in ctx.sitemap_urls]
        yield warning(
            "structure.orphan_pages",
            f"{len(orphans)} page(s) have no internal links pointing at them",
            evidence=sample(orphans),
            affected=len(orphans),
            fix="Orphans are reachable only if a crawler already knows the URL. "
                + (f"{len(from_sitemap)} of them are in the sitemap, which is how they were "
                   "found here — but a sitemap entry is a much weaker signal than a link. "
                   if from_sitemap else "")
                + "Link to them from somewhere relevant.",
        )

    if deep := [(url, depth) for url, depth in graph.click_depth.items()
                if depth > MAX_GOOD_CLICK_DEPTH]:
        deep.sort(key=lambda item: -item[1])
        yield notice(
            "structure.deep_pages",
            f"{len(deep)} page(s) are more than {MAX_GOOD_CLICK_DEPTH} clicks from the homepage",
            evidence=sample([f"{url} ({depth} clicks)" for url, depth in deep], 4),
            affected=len(deep),
            fix="Deep pages are crawled less often and treated as less important. Surface them "
                "from category pages, navigation, or related-content links.",
        )

    if dead := graph.dead_ends():
        yield notice(
            "structure.dead_ends",
            f"{len(dead)} page(s) link nowhere else on the site",
            evidence=sample(dead),
            affected=len(dead),
            fix="A page with no onward internal links ends the crawl path and passes on no "
                "authority. Add contextual links to related pages.",
        )


@site_analyzer
def internal_link_health(ctx: SiteContext) -> Iterator[Finding]:
    """Attribute broken and redirecting links back to the pages containing them."""
    by_url = {normalize(p.requested_url): p for p in ctx.pages}

    broken: dict[str, list[str]] = defaultdict(list)
    redirecting: dict[str, list[str]] = defaultdict(list)

    for page in ctx.pages:
        for target in page.outlinks:
            destination = by_url.get(target)
            if destination is None:
                continue  # never fetched — the budget stopped short, not a fault
            if destination.error is not None or (destination.status or 0) >= 400:
                state = destination.error.value if destination.error else str(destination.status)
                broken[f"{target} ({state})"].append(page.final_url)
            elif destination.redirects:
                redirecting[f"{target} -> {destination.final_url}"].append(page.final_url)

    if broken:
        evidence = [f"{target}  linked from {sources[0]}"
                    + (f" (+{len(sources) - 1} more)" if len(sources) > 1 else "")
                    for target, sources in broken.items()]
        yield critical(
            "structure.broken_internal_links",
            f"{len(broken)} internal link target(s) are broken",
            evidence=sample(evidence, 4),
            affected=len({source for sources in broken.values() for source in sources}),
            fix="Fix or remove the links. Broken internal links waste crawl budget and send "
                "visitors to dead ends.",
        )

    if redirecting:
        evidence = [f"{target}  linked from {sources[0]}"
                    + (f" (+{len(sources) - 1} more)" if len(sources) > 1 else "")
                    for target, sources in redirecting.items()]
        yield notice(
            "structure.links_to_redirects",
            f"{len(redirecting)} internal link target(s) redirect",
            evidence=sample(evidence, 4),
            affected=len({source for sources in redirecting.values() for source in sources}),
            fix="Point internal links straight at the destination. Every hop costs crawl "
                "budget and adds latency for visitors.",
        )


@site_analyzer
def soft_404s(ctx: SiteContext) -> Iterator[Finding]:
    """Pages that answer 200 while really being a not-found page.

    Detected by asking the site for a URL that cannot exist and fingerprinting
    whatever comes back. If that probe returns 200, any page matching it is a
    soft 404.
    """
    if not ctx.soft_404_fingerprint:
        return

    suspects = [
        page.final_url for page in ctx.pages
        if page.ok and page.sketch and near_duplicate(page.sketch, ctx.soft_404_fingerprint)
    ]
    yield warning(
        "structure.soft_404",
        f"The site answers 200 for URLs that do not exist"
        + (f", and {len(suspects)} crawled page(s) match that not-found page" if suspects else ""),
        evidence=sample(suspects, 4) if suspects else "probe of a random URL returned 200",
        fix="Return a real 404 (or 410) status for missing pages. A 200 tells search engines "
            "to index the error page, and hides genuinely broken links from every audit tool.",
    )


@site_analyzer
def crawl_waste(ctx: SiteContext) -> Iterator[Finding]:
    """Query-string variants of one path: the classic faceted-navigation trap."""
    by_path: dict[str, set[str]] = defaultdict(set)
    for page in ctx.pages:
        parts = urlsplit(page.requested_url)
        if parts.query:
            by_path[f"{parts.scheme}://{parts.netloc}{parts.path}"].add(parts.query)

    exploding = {path: queries for path, queries in by_path.items() if len(queries) >= 5}
    if exploding:
        worst = sorted(exploding.items(), key=lambda item: -len(item[1]))
        yield warning(
            "crawl.parameter_explosion",
            f"{len(exploding)} path(s) were crawled with many different query strings",
            evidence=sample([f"{path} ({len(queries)} variants)" for path, queries in worst], 3),
            affected=sum(len(queries) for queries in exploding.values()),
            fix="Faceted navigation and filters can generate effectively unlimited URLs. "
                "Canonicalise the variants to the base page, or disallow the parameters in "
                "robots.txt, or the crawl budget goes on filter combinations instead of content.",
        )


@site_analyzer
def authority_distribution(ctx: SiteContext) -> Iterator[Finding]:
    """Report where internal authority pools. No API needed — it is our own link graph."""
    graph = ctx.graph
    if len(graph.nodes) < 3:
        return

    top = graph.top_by_pagerank(5)
    yield info(
        "structure.internal_pagerank",
        "Internal authority concentrates on: "
        + ", ".join(f"{url} ({score:.1%})" for url, score in top),
        evidence=f"{len(graph.nodes)} pages, "
                 f"{sum(len(t) for t in graph.outgoing.values())} internal links, "
                 f"deepest page {max(graph.click_depth.values(), default=0)} clicks from home",
    )


@site_analyzer
def external_link_health(ctx: SiteContext) -> Iterator[Finding]:
    """Only runs with --check-external, since it means requests to other people's servers."""
    if not ctx.external_links:
        return

    sources: dict[str, list[str]] = defaultdict(list)
    for page in ctx.pages:
        for target in page.external_links:
            sources[target].append(page.final_url)

    broken: list[str] = []
    for url, page in ctx.external_links.items():
        if page.error is not None:
            state = page.error.value
        elif (page.status or 0) >= 400:
            state = str(page.status)
        else:
            continue
        origin = sources.get(url, ["?"])[0]
        broken.append(f"{url} ({state}) linked from {origin}")

    yield info(
        "external.checked",
        f"Checked {len(ctx.external_links)} external link(s)",
    )
    if broken:
        yield warning(
            "external.broken_links",
            f"{len(broken)} external link(s) do not resolve",
            evidence=sample(broken, 4),
            affected=len({page for url in ctx.external_links for page in sources.get(url, [])}),
            fix="Update or remove them. Note that some sites block automated requests, so "
                "confirm a failure in a browser before deleting a link.",
        )
