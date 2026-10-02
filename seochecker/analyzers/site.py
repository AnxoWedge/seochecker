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

# Answer engines. These are the crawlers that fetch a page in order to *cite* it
# in an AI answer, and blocking one costs visibility exactly the way blocking a
# search engine does.
#
# They are deliberately separate from the training crawlers below, because the
# vendors made them separate: you can decline to feed model training and still be
# quotable in ChatGPT, Claude or Perplexity. Treating the two as one thing — which
# this tool did — hides a real loss behind an editorial choice.
ANSWER_ENGINES = {
    "oai-searchbot": "ChatGPT search",
    "chatgpt-user": "ChatGPT (user-initiated fetches)",
    "claude-searchbot": "Claude search",
    "claude-user": "Claude (user-initiated fetches)",
    "perplexitybot": "Perplexity",
    "perplexity-user": "Perplexity (user-initiated fetches)",
}

# Training crawlers. Blocking these is a choice about your content, not a
# visibility problem, so it is recorded rather than judged.
TRAINING_CRAWLERS = {
    "gptbot": "OpenAI model training",
    "google-extended": "Google Gemini training",
    "claudebot": "Anthropic model training",
    "applebot-extended": "Apple model training",
    "ccbot": "Common Crawl",
    "bytespider": "ByteDance",
}


@site_analyzer
def robots_blocks_search_engines(ctx: SiteContext) -> Iterator[Finding]:
    """Named crawlers that robots.txt shuts out of the whole site."""
    robots = ctx.robots
    if not robots.fetched:
        return

    blocked = [label for token, label in SEARCH_CRAWLERS.items()
               if not robots.is_allowed(ctx.config.url, token)]
    if blocked:
        yield critical(
            "robots.blocks_search_engine",
            f"robots.txt blocks {len(blocked)} search engine(s) from the site",
            evidence=", ".join(blocked),
            fix="These crawlers are being told not to fetch this URL at all, so the site "
                "cannot appear in their results. Remove the disallow rule unless that is "
                "genuinely intended.",
        )

    shut_out = [label for token, label in ANSWER_ENGINES.items()
                if not robots.is_allowed(ctx.config.url, token)]
    if shut_out:
        yield warning(
            "robots.blocks_answer_engines",
            f"robots.txt blocks {len(shut_out)} AI answer engine(s)",
            evidence=", ".join(shut_out),
            fix="These fetch pages in order to cite them in answers, and are separate from "
                "the crawlers that collect training data. Blocking them removes the site "
                "from AI answers without protecting it from training — which is usually the "
                "opposite of what was intended.",
        )

    training = [label for token, label in TRAINING_CRAWLERS.items()
                if not robots.is_allowed(ctx.config.url, token)]
    if training:
        yield info(
            "robots.blocks_ai_training",
            f"robots.txt blocks {len(training)} AI training crawler(s)",
            evidence=", ".join(training),
            fix="Recorded, not judged: declining to feed model training is a decision about "
                "your content. It does not affect whether answer engines can cite you.",
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


@site_analyzer
def measurement(ctx: SiteContext) -> Iterator[Finding]:
    """Whether anything on this site reports traffic.

    A site-level question, and it has to be: only a sample of pages is
    fingerprinted, so asking it per page meant it silently never fired on any
    page past the sample.

    A consent gate changes the answer entirely. This crawler never accepts
    cookies and neither does a search engine crawler, so on a site that holds its
    tags behind consent there is nothing to find before consent — and calling
    that "no analytics" is simply wrong. It was, for a site running three GA4
    properties.
    """
    # Gate on having looked, not on having found something: detecting no
    # technology at all is precisely the case this check exists for.
    if not ctx.html_pages:
        return
    categories = {tech.category for tech in ctx.technologies}
    if categories & {"analytics", "tag-manager"}:
        return

    if "consent" in categories:
        gate = ", ".join(sorted(t.name for t in ctx.technologies
                                if t.category == "consent"))
        yield info(
            "technical.analytics_behind_consent",
            "No analytics loads before cookie consent",
            evidence=f"consent gate: {gate}",
            fix="Expected, and not a fault: this crawler never accepts cookies, and neither "
                "does a search engine crawler. Whether analytics fires for real visitors "
                "cannot be told from here — check in a browser after accepting.",
        )
        return

    yield notice(
        "technical.no_analytics",
        "No analytics or tag manager detected anywhere on the site",
        fix="Nothing here reports traffic, so SEO work cannot be measured. No consent banner "
            "was found either, so this is unlikely to be tags waiting on consent — but it is "
            "worth confirming in a browser.",
    )


@site_analyzer
def llms_txt(ctx: SiteContext) -> Iterator[Finding]:
    """Whether the site publishes /llms.txt.

    Reported honestly, which means quietly. The major AI *search* crawlers —
    GPTBot, ClaudeBot, PerplexityBot, OAI-SearchBot — overwhelmingly ignore the
    file today and read the HTML instead, so this is not the lever some people
    claim. What does fetch it are coding agents and in-product assistants, which
    is a real and growing audience, and the file costs an afternoon.
    """
    if not ctx.llms_txt:
        return
    if ctx.llms_txt.get("present"):
        yield info(
            "robots.llms_txt_found",
            f"/llms.txt is published ({ctx.llms_txt['bytes']:,} bytes)",
        )
        return

    yield notice(
        "robots.no_llms_txt",
        "No /llms.txt",
        evidence=f"HTTP {ctx.llms_txt.get('status') or 'no response'}",
        fix="A plain-text map of what the site is and where its important pages are. "
            "Today's AI search crawlers mostly ignore it and read the HTML, so this is not "
            "urgent — but coding agents and in-product assistants do fetch it, and it costs "
            "very little to publish.",
    )
