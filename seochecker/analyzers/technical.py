"""Transport, delivery and mobile-readiness — everything outside the content."""

from __future__ import annotations

from typing import Iterator
from urllib.parse import urlsplit

from ..models import Finding
from .base import PageContext, analyzer, critical, info, notice, sample, warning


BLOCK_ADVICE = (
    "The site answered with a bot-mitigation challenge, so nothing below reflects the real "
    "page. Slow the crawl down (--delay 2), and if you own or have permission to audit this "
    "site, ask whoever manages the WAF to allow this crawler's user agent or source IP. "
    "seochecker does not try to defeat challenges."
)

FAILURE_ADVICE = {
    "timeout": "The server did not respond in time. Try --timeout 60; if it persists, the "
               "page is too slow for crawlers as well as for visitors.",
    "connection": "Could not connect. Check the hostname resolves and the server is reachable.",
    "tls": "The TLS certificate could not be verified. Browsers will show a security warning, "
           "and search engines will not index the page.",
    "redirect_loop": "The URL redirects back to itself. Search engines abandon the page.",
    "too_many_redirects": "Too many redirect hops. Collapse the chain to one.",
    "invalid_url": "The URL could not be parsed.",
}


@analyzer(when_errored=True)
def fetch_outcome(ctx: PageContext) -> Iterator[Finding]:
    """When the fetch failed, say so — and say nothing else about the page."""
    error = ctx.page.error
    if error is None:
        return

    if error.value == "blocked":
        yield critical(
            "technical.blocked",
            "Blocked by bot mitigation — the page was not audited",
            evidence=f"{ctx.page.status} {ctx.page.reason}: {ctx.page.error_detail}",
            fix=BLOCK_ADVICE,
        )
        return

    yield critical(
        "technical.fetch_failed",
        f"Could not fetch the page ({error.value})",
        evidence=ctx.page.error_detail,
        fix=FAILURE_ADVICE.get(error.value, "Check the URL and the server."),
    )


@analyzer
def http_status(ctx: PageContext) -> Iterator[Finding]:
    status = ctx.page.status
    if status is None:
        return
    if 500 <= status:
        yield critical(
            "technical.server_error",
            f"Server returned {status} {ctx.page.reason}",
            fix="A 5xx page cannot rank and, repeated across a site, slows the crawl rate for "
                "everything else.",
        )
    elif 400 <= status:
        yield critical(
            "technical.client_error",
            f"Page returns {status} {ctx.page.reason}",
            fix="Restore the page, or redirect it to the closest equivalent. Check what links "
                "to it before deleting.",
        )


@analyzer
def transport_security(ctx: PageContext) -> Iterator[Finding]:
    if not ctx.https:
        yield critical(
            "technical.not_https",
            "Page is served over plain HTTP",
            fix="Serve over HTTPS and redirect http:// to it. HTTPS has been a ranking signal "
                "since 2014, and browsers mark HTTP pages as not secure.",
        )
        return

    if not ctx.page.header("strict-transport-security"):
        yield notice(
            "technical.no_hsts",
            "No Strict-Transport-Security header",
            fix="Add HSTS so browsers refuse to fall back to HTTP. It also removes one "
                "redirect hop for returning visitors.",
        )

    if not ctx.doc:
        return
    insecure = [url for _, url in ctx.doc.subresources if urlsplit(url).scheme == "http"]
    if insecure:
        yield critical(
            "technical.mixed_content",
            f"{len(insecure)} resource(s) loaded over http:// on an https:// page",
            evidence=sample(insecure),
            fix="Browsers block or downgrade mixed content, so these assets may simply not "
                "load. Serve everything over https://.",
        )


@analyzer
def redirects(ctx: PageContext) -> Iterator[Finding]:
    hops = ctx.page.redirects
    if not hops:
        return
    chain = " -> ".join([hops[0].url] + [hop.location for hop in hops])
    if len(hops) >= ctx.thresholds.redirect_chain_warn:
        yield warning(
            "technical.redirect_chain",
            f"{len(hops)} redirects before reaching the page",
            evidence=chain,
            fix="Collapse the chain to a single hop. Each extra hop costs crawl budget and "
                "adds latency for the visitor.",
        )
    else:
        yield notice(
            "technical.redirect",
            f"URL redirects ({hops[0].status})",
            evidence=chain,
            fix="Fine as a one-off. Update internal links to point at the destination so the "
                "hop is not paid on every visit.",
        )

    if temporary := [hop for hop in hops if hop.status in (302, 303, 307)]:
        yield notice(
            "technical.temporary_redirect",
            f"{len(temporary)} temporary redirect(s) in the chain",
            evidence=sample([f"{hop.status} {hop.url}" for hop in temporary], 3),
            fix="Use 301 for a permanent move — a temporary redirect tells search engines to "
                "keep the old URL indexed.",
        )


@analyzer
def mobile_and_encoding(ctx: PageContext) -> Iterator[Finding]:
    if not ctx.doc:
        return

    if not ctx.doc.viewport:
        yield critical(
            "technical.no_viewport",
            "No viewport meta tag",
            fix="Add <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">. "
                "Without it, mobile browsers render at desktop width and Google indexes the "
                "mobile version first.",
        )
    elif "width=device-width" not in ctx.doc.viewport.replace(" ", ""):
        yield warning(
            "technical.viewport_fixed",
            "Viewport does not set width=device-width",
            evidence=ctx.doc.viewport,
            fix="A fixed-width viewport forces horizontal scrolling on phones.",
        )

    _, header_charset = (ctx.page.content_type.partition(";")[0], ctx.page.charset)
    if not ctx.doc.charset_meta and ctx.page.charset_source not in ("header", "bom"):
        yield warning(
            "technical.no_charset",
            "Character encoding is not declared",
            evidence=f"guessed {ctx.page.charset} ({ctx.page.charset_source})",
            fix="Declare it — <meta charset=\"utf-8\"> as the first thing in <head>, or in the "
                "Content-Type header. Guessed encodings are where mojibake comes from.",
        )


@analyzer
def delivery(ctx: PageContext) -> Iterator[Finding]:
    page = ctx.page
    limits = ctx.thresholds

    if page.timing.ttfb_ms is not None:
        if page.timing.ttfb_ms > limits.ttfb_very_slow_ms:
            yield warning(
                "technical.ttfb_very_slow",
                f"Server took {page.timing.ttfb_ms:.0f}ms to respond",
                fix="Above ~1.8s the delay dominates every other performance fix. Look at "
                    "server-side caching, database queries and hosting.",
            )
        elif page.timing.ttfb_ms > limits.ttfb_slow_ms:
            yield notice(
                "technical.ttfb_slow",
                f"Server took {page.timing.ttfb_ms:.0f}ms to respond",
                fix=f"Google's threshold for a good response time is {limits.ttfb_slow_ms:.0f}ms.",
            )

    if page.is_html and not page.header("content-encoding"):
        yield warning(
            "technical.no_compression",
            "HTML is served uncompressed",
            evidence=f"{page.wire_bytes:,} bytes over the wire",
            fix="Enable gzip or brotli. It is usually one line of server config and cuts HTML "
                "transfer by 70-80%.",
        )

    if page.decoded_bytes > limits.html_bytes_warn:
        yield notice(
            "technical.large_html",
            f"HTML document is {page.decoded_bytes / 1024:.0f} KB",
            fix="Large HTML delays first paint. Usually inline styles, inline data blobs or "
                "generated markup that could be trimmed.",
        )

    if page.http_version and page.http_version.upper() in ("HTTP/1.0", "HTTP/1.1"):
        yield notice(
            "technical.http1",
            f"Served over {page.http_version}",
            fix="HTTP/2 or HTTP/3 multiplexes requests over one connection, which matters most "
                "on pages with many assets.",
        )

    if not (page.header("etag") or page.header("last-modified")):
        yield notice(
            "technical.no_cache_validators",
            "No ETag or Last-Modified header",
            fix="Validators let browsers and crawlers revalidate cheaply with a 304 instead of "
                "re-downloading the page.",
        )

    if page.truncated:
        yield info(
            "technical.body_truncated",
            "Response body exceeded the size limit and was cut short",
            evidence=f"limit is {ctx.config.max_bytes:,} bytes",
            fix="Analysis of this page is partial. Raise --max-bytes to audit it fully.",
        )


@analyzer
def render_path(ctx: PageContext) -> Iterator[Finding]:
    if not ctx.doc:
        return

    blocking = [script.src for script in ctx.doc.scripts if script.render_blocking]
    if len(blocking) >= ctx.thresholds.render_blocking_warn:
        yield notice(
            "technical.render_blocking_scripts",
            f"{len(blocking)} render-blocking script(s) in <head>",
            evidence=sample(blocking),
            fix="Add defer (or async where order does not matter). Each blocking script stops "
                "the parser until it downloads and runs.",
        )

    if not ctx.doc.favicons:
        yield notice(
            "technical.no_favicon",
            "No favicon declared",
            fix="Add <link rel=\"icon\">. Browsers fall back to /favicon.ico, but Google also "
                "shows the favicon in mobile results.",
        )


@analyzer
def measurement(ctx: PageContext) -> Iterator[Finding]:
    """No analytics and no tag manager means nothing about this page is measured."""
    if not ctx.doc or not ctx.technologies:
        return
    categories = {tech.category for tech in ctx.technologies}
    if categories & {"analytics", "tag-manager"}:
        return
    yield notice(
        "technical.no_analytics",
        "No analytics or tag manager detected",
        fix="Nothing here reports traffic, so SEO work on this page cannot be measured. "
            "If analytics loads through a consent manager, it may simply not fire before "
            "consent — worth confirming by hand.",
    )
