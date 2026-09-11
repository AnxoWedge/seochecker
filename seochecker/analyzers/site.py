"""Whole-crawl checks: robots.txt and sitemap health.

These need the finished crawl, not a single page — a sitemap entry that 404s is
only visible once you have both the sitemap and the fetch result for that URL.
"""

from __future__ import annotations

from typing import Iterator
from urllib.parse import urlsplit

from ..models import Finding
from ..urls import normalize
from .base import (
    SiteContext, critical, info, notice, parse_directives, sample, site_analyzer, warning,
)


@site_analyzer
def robots_health(ctx: SiteContext) -> Iterator[Finding]:
    robots = ctx.robots

    if robots.error:
        yield warning(
            "robots.unreachable",
            "robots.txt could not be fetched",
            evidence=f"{robots.source_url}: {robots.error}",
            fix="Search engines treat a persistently failing robots.txt as a signal to stop "
                "crawling the site. A 404 is fine; a 5xx or a timeout is not.",
        )
        return

    if not robots.fetched:
        yield notice(
            "robots.missing",
            f"No robots.txt ({robots.status or 'not found'})",
            fix="Not required, but it is where you declare your sitemap and keep crawlers out "
                "of search, cart and admin URLs.",
        )
        return

    agent_group = robots._group_for("*")
    if agent_group:
        blanket = [r for r in agent_group.rules if not r.allow and r.path == "/"]
        if blanket:
            yield critical(
                "robots.blocks_everything",
                "robots.txt disallows the entire site for all crawlers",
                evidence="User-agent: *  Disallow: /",
                fix="Unless this is a staging site, remove it. Nothing on this domain can be "
                    "crawled while it is there.",
            )

    if not robots.sitemaps:
        yield notice(
            "robots.no_sitemap_declared",
            "robots.txt does not declare a sitemap",
            fix="Add a `Sitemap:` line with the absolute URL. It is the most reliable way for "
                "a search engine to find every page you care about.",
        )


@site_analyzer
def sitemap_health(ctx: SiteContext) -> Iterator[Finding]:
    sitemap = ctx.sitemap

    if not sitemap.fetched:
        reasons = sample([f"{url} ({why})" for url, why in sitemap.failed], 3)
        yield warning(
            "sitemap.missing",
            "No usable sitemap found",
            evidence=reasons or "nothing at /sitemap.xml and none declared in robots.txt",
            fix="Publish an XML sitemap and declare it in robots.txt. Without one, discovery "
                "depends entirely on internal linking.",
        )
        return

    yield info(
        "sitemap.found",
        f"{len(sitemap.entries)} URL(s) across {len(sitemap.fetched)} sitemap file(s)",
        evidence=sample(sitemap.fetched, 3),
    )

    if sitemap.failed:
        yield warning(
            "sitemap.partly_unreadable",
            f"{len(sitemap.failed)} sitemap file(s) could not be read",
            evidence=sample([f"{url} ({why})" for url, why in sitemap.failed], 3),
            fix="A sitemap listed in an index that does not load means those URLs are not "
                "being submitted at all.",
        )

    if sitemap.truncated:
        yield info(
            "sitemap.truncated",
            "Sitemap parsing stopped at the configured limit",
            fix="Raise --max-pages to cover more of the sitemap.",
        )


@site_analyzer
def sitemap_entries(ctx: SiteContext) -> Iterator[Finding]:
    """Cross-check what the sitemap promises against what the crawl actually got."""
    if not ctx.sitemap_urls:
        return

    broken: list[str] = []
    redirected: list[str] = []
    noindexed: list[str] = []
    non_canonical: list[str] = []

    for page in ctx.pages:
        if normalize(page.requested_url) not in ctx.sitemap_urls:
            continue
        if page.error is not None:
            broken.append(f"{page.requested_url} ({page.error.value})")
            continue
        if page.status and page.status >= 400:
            broken.append(f"{page.requested_url} ({page.status})")
            continue
        if page.redirects:
            redirected.append(f"{page.requested_url} -> {page.final_url}")

        directives = parse_directives(
            [page.header("x-robots-tag")] if page.header("x-robots-tag") else []
        )
        if page.html and page.is_html:
            from ..html import Document
            doc = Document(page.html, page.final_url)
            directives |= parse_directives(doc.meta_all("robots") + doc.meta_all("googlebot"))
            if doc.canonical and doc.canonical.rstrip("/") != page.final_url.rstrip("/"):
                non_canonical.append(f"{page.final_url} -> {doc.canonical}")
        if directives & {"noindex", "none"}:
            noindexed.append(page.requested_url)

    if broken:
        yield warning(
            "sitemap.broken_entries",
            f"{len(broken)} sitemap URL(s) do not resolve",
            evidence=sample(broken),
            affected=len(broken),
            fix="A sitemap is a list of pages you are asking to be indexed. Dead entries waste "
                "crawl budget and reduce trust in the whole file.",
        )
    if redirected:
        yield notice(
            "sitemap.redirecting_entries",
            f"{len(redirected)} sitemap URL(s) redirect",
            evidence=sample(redirected),
            affected=len(redirected),
            fix="List the destination URL directly. A sitemap should contain final URLs.",
        )
    if noindexed:
        yield warning(
            "sitemap.noindexed_entries",
            f"{len(noindexed)} sitemap URL(s) are set to noindex",
            evidence=sample(noindexed),
            affected=len(noindexed),
            fix="Contradictory signals: the sitemap asks for indexing while the page refuses "
                "it. Remove them from the sitemap, or remove the noindex.",
        )
    if non_canonical:
        yield notice(
            "sitemap.non_canonical_entries",
            f"{len(non_canonical)} sitemap URL(s) canonicalise elsewhere",
            evidence=sample(non_canonical),
            affected=len(non_canonical),
            fix="List canonical URLs only, or the sitemap is nominating pages that point away "
                "from themselves.",
        )


@site_analyzer
def crawl_shape(ctx: SiteContext) -> Iterator[Finding]:
    skipped = ctx.frontier.get("skipped", {})
    if blocked := skipped.get("blocked by robots.txt"):
        yield notice(
            "crawl.blocked_by_robots",
            f"{blocked} discovered URL(s) are disallowed by robots.txt",
            fix="Expected for cart, search and admin paths. Worth checking none of them are "
                "pages you want indexed.",
        )

    crawled = [p for p in ctx.pages if p.error is None]
    if len(crawled) <= 1 and ctx.config.max_pages > 1:
        yield warning(
            "crawl.no_pages_discovered",
            "The crawl found no further pages from the starting URL",
            evidence=f"skipped: {skipped}" if skipped else "no links in scope",
            fix="Either the page has no internal links a crawler can follow, or scope rules "
                "excluded them. If the site is JavaScript-rendered, the links may not be in "
                "the served HTML at all.",
        )


# Search crawlers whose exclusion costs visibility, and the token each reads in
# robots.txt. Qwant runs Qwantbot (Qwantify is the older token).
SEARCH_CRAWLERS = {
    "googlebot": "Google",
    "bingbot": "Bing",
    "applebot": "Apple (Siri, Spotlight, Safari)",
    "duckduckbot": "DuckDuckGo",
    "qwantbot": "Qwant",
    "qwantify": "Qwant (legacy token)",
    "yandex": "Yandex",
    "baiduspider": "Baidu",
    "slurp": "Yahoo",
}

# Blocking these is a legitimate editorial choice, not a mistake, so it is
# reported as information rather than a problem.
AI_CRAWLERS = {
    "gptbot": "OpenAI",
    "google-extended": "Google AI training",
    "applebot-extended": "Apple AI training",
    "ccbot": "Common Crawl",
    "claudebot": "Anthropic",
    "perplexitybot": "Perplexity",
    "bytespider": "ByteDance",
}


@site_analyzer
def robots_blocks_search_engines(ctx: SiteContext) -> Iterator[Finding]:
    """Named crawlers that robots.txt shuts out of the whole site."""
    robots = ctx.robots
    if not robots.fetched:
        return

    blocked_search = [
        label for token, label in SEARCH_CRAWLERS.items()
        if not robots.is_allowed(ctx.config.url, token)
    ]
    if blocked_search:
        yield critical(
            "robots.blocks_search_engine",
            f"robots.txt blocks {len(blocked_search)} search engine(s) from the site",
            evidence=", ".join(blocked_search),
            fix="These crawlers are being told not to fetch this URL at all, so the site "
                "cannot appear in their results. Remove the disallow rule unless that is "
                "genuinely intended.",
        )

    blocked_ai = [
        label for token, label in AI_CRAWLERS.items()
        if not robots.is_allowed(ctx.config.url, token)
    ]
    if blocked_ai:
        yield info(
            "robots.blocks_ai_crawlers",
            f"robots.txt blocks {len(blocked_ai)} AI crawler(s)",
            evidence=", ".join(blocked_ai),
            fix="Recorded for completeness — whether to allow AI crawlers is an editorial "
                "decision, not an SEO problem.",
        )


@site_analyzer
def robots_blocks_page_resources(ctx: SiteContext) -> Iterator[Finding]:
    """CSS and JavaScript a renderer needs, disallowed in robots.txt.

    Google: "if the absence of these resources make the page harder for Google's
    crawler to understand the page, don't block them". Apple asks for the same:
    Applebot needs the JavaScript and CSS required to render a page.
    """
    if not ctx.robots.fetched:
        return

    blocked: list[str] = []
    seen: set[str] = set()
    for page in ctx.pages:
        for resource in (page.seo or {}).get("resources") or []:
            if resource in seen:
                continue
            seen.add(resource)
            if not ctx.robots.is_allowed(resource, "googlebot"):
                blocked.append(resource)

    if blocked:
        yield warning(
            "robots.blocks_resources",
            f"robots.txt disallows {len(blocked)} CSS or JavaScript file(s) the pages need",
            evidence=sample(blocked, 4),
            affected=len(blocked),
            fix="Crawlers render pages to understand them, and cannot render what they are "
                "not allowed to fetch. Google and Apple both ask that the CSS and "
                "JavaScript required to display a page stay crawlable.",
        )
