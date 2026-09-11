"""Title, description, indexability and canonical — the head of the document."""

from __future__ import annotations

from typing import Iterator
from urllib.parse import urlsplit

from ..models import Finding
from .base import PageContext, analyzer, critical, info, notice, parse_directives, warning

BLOCKING_DIRECTIVES = {"noindex", "none"}


@analyzer
def title(ctx: PageContext) -> Iterator[Finding]:
    if not ctx.doc:
        return
    titles = [t for t in ctx.doc.titles if t.strip()]

    if not titles:
        yield critical(
            "title.missing",
            "Page has no title",
            fix="Add a unique <title> in <head>. It is the single strongest on-page signal "
                "and the clickable line in search results.",
        )
        return

    if len(ctx.doc.titles) > 1:
        yield warning(
            "title.multiple",
            f"{len(ctx.doc.titles)} <title> tags — search engines use the first",
            evidence=" | ".join(ctx.doc.titles[:3]),
            fix="Keep exactly one <title>.",
        )

    text = titles[0]
    length = len(text)
    limits = ctx.thresholds
    if length > limits.title_max:
        yield warning(
            "title.too_long",
            f"Title is {length} characters — likely truncated in results",
            evidence=text,
            fix=f"Trim to about {limits.title_max} characters, front-loading the key terms.",
        )
    elif length < limits.title_min:
        yield notice(
            "title.too_short",
            f"Title is only {length} characters",
            evidence=text,
            fix=f"Aim for {limits.title_min}–{limits.title_max} characters; you have room "
                "for a descriptive phrase plus the brand.",
        )


@analyzer
def description(ctx: PageContext) -> Iterator[Finding]:
    if not ctx.doc:
        return
    descriptions = ctx.doc.meta_all("description")
    filled = [d for d in descriptions if d.strip()]

    if not filled:
        yield warning(
            "description.missing",
            "No meta description",
            fix="Write one. Google may ignore it, but when it uses it you control the "
                "sales pitch in the result.",
        )
        return

    if len(descriptions) > 1:
        yield warning(
            "description.multiple",
            f"{len(descriptions)} meta descriptions",
            evidence=" | ".join(d[:60] for d in descriptions[:3]),
            fix="Keep exactly one.",
        )

    text = filled[0]
    length = len(text)
    limits = ctx.thresholds
    if length > limits.description_max:
        yield notice(
            "description.too_long",
            f"Meta description is {length} characters — likely truncated",
            evidence=text,
            fix=f"Trim to about {limits.description_max} characters.",
        )
    elif length < limits.description_min:
        yield notice(
            "description.too_short",
            f"Meta description is only {length} characters",
            evidence=text,
            fix=f"Aim for {limits.description_min}–{limits.description_max} characters.",
        )


@analyzer
def indexability(ctx: PageContext) -> Iterator[Finding]:
    sources: list[str] = []
    values: list[str] = []

    if ctx.doc:
        for name in ("robots", "googlebot"):
            for content in ctx.doc.meta_all(name):
                if content:
                    values.append(content)
                    sources.append(f"meta {name}: {content}")

    if header := ctx.page.header("x-robots-tag"):
        values.append(header)
        sources.append(f"X-Robots-Tag: {header}")

    if not values:
        return

    directives = parse_directives(values)

    if directives & BLOCKING_DIRECTIVES:
        yield critical(
            "indexability.noindex",
            "Page is set to noindex — it will not appear in search results",
            evidence="; ".join(sources),
            fix="If this page should rank, remove the noindex directive. If it should not, "
                "this is working as intended.",
        )
    if "nofollow" in directives or "none" in directives:
        yield warning(
            "indexability.nofollow",
            "Page-level nofollow — no link on this page passes authority",
            evidence="; ".join(sources),
            fix="Remove the nofollow directive unless this page is deliberately a dead end.",
        )
    if extras := directives & {"noarchive", "nosnippet", "noimageindex", "notranslate"}:
        yield info(
            "indexability.restricted",
            f"Result presentation is restricted: {', '.join(sorted(extras))}",
            evidence="; ".join(sources),
        )


@analyzer
def canonical(ctx: PageContext) -> Iterator[Finding]:
    if not ctx.doc:
        return
    canonicals = ctx.doc.canonicals

    if not canonicals:
        yield warning(
            "canonical.missing",
            "No canonical URL declared",
            fix="Add <link rel=\"canonical\"> pointing at this page's preferred URL. It is "
                "the cheapest defence against duplicate content from parameters and "
                "www/non-www variants.",
        )
        return

    if len(canonicals) > 1:
        yield warning(
            "canonical.multiple",
            f"{len(canonicals)} conflicting canonical tags — search engines may ignore all of them",
            evidence=" | ".join(canonicals[:3]),
            fix="Keep exactly one canonical.",
        )

    target = canonicals[0]
    raw = ctx.doc.canonical_raw
    if not raw.lower().startswith(("http://", "https://")):
        yield notice(
            "canonical.relative",
            "Canonical URL is relative",
            evidence=raw,
            fix="Use an absolute URL — relative canonicals resolve differently if the page "
                "is ever served from another path.",
        )

    parts = urlsplit(target)
    if parts.netloc.lower() != ctx.host:
        yield warning(
            "canonical.cross_domain",
            "Canonical points to a different domain — this page cedes ranking to it",
            evidence=target,
            fix="Confirm this is intentional. If it is not, point the canonical at this domain.",
        )
    elif ctx.https and parts.scheme == "http":
        yield warning(
            "canonical.insecure",
            "Canonical points to the http:// version of an https:// page",
            evidence=target,
            fix="Change the canonical to https://.",
        )
    elif target.rstrip("/") != ctx.url.rstrip("/"):
        yield notice(
            "canonical.not_self_referential",
            "Canonical points to a different URL on this domain",
            evidence=f"page: {ctx.url} -> canonical: {target}",
            fix="Fine if this page is a deliberate duplicate. Otherwise the canonical should "
                "point at this URL.",
        )
