"""Outgoing links: anchor quality, safety, and whether the page is a dead end."""

from __future__ import annotations

from typing import Iterator
from urllib.parse import urlsplit

from ..models import Finding
from .base import PageContext, analyzer, notice, sample, warning

# Anchor text that tells a search engine — and a screen-reader user tabbing
# through links — nothing at all. English and Portuguese, since both turn up.
GENERIC_ANCHORS = frozenset({
    "click here", "click", "here", "read more", "learn more", "more", "more info",
    "this", "this page", "link", "download", "see more", "view", "go", "details",
    "clique aqui", "clica aqui", "aqui", "saiba mais", "sabe mais", "leia mais",
    "ler mais", "ver mais", "ver", "mais", "continuar", "detalhes", "descarregar",
})


@analyzer
def link_quality(ctx: PageContext) -> Iterator[Finding]:
    if not ctx.doc:
        return
    links = ctx.doc.links
    if not links:
        yield warning(
            "links.none",
            "Page has no links at all",
            fix="A page with no outgoing links is a crawl dead end and passes no authority on.",
        )
        return

    internal = [link for link in links if link.internal]
    if not internal:
        yield warning(
            "links.no_internal",
            f"No internal links ({len(links)} links, all external)",
            fix="Link to related pages on this site so crawlers can reach them and authority "
                "flows through the site.",
        )

    if empty := [link.url for link in links if not link.anchor.strip()]:
        yield notice(
            "links.empty_anchor",
            f"{len(empty)} link(s) with no anchor text",
            evidence=sample(empty),
            fix="Give the link text, or an aria-label. If it wraps an image, give the image "
                "an alt.",
        )

    generic = [f"{link.anchor!r} -> {link.url}" for link in links
               if link.anchor.strip().lower().strip(" .!→>»") in GENERIC_ANCHORS]
    if generic:
        yield notice(
            "links.generic_anchor",
            f"{len(generic)} link(s) with generic anchor text",
            evidence=sample(generic, 3),
            fix="Anchor text describes the destination — 'our pricing plans', not 'read more'.",
        )

    if len(links) > ctx.thresholds.max_links:
        yield notice(
            "links.excessive",
            f"{len(links)} links on one page",
            fix=f"Past roughly {ctx.thresholds.max_links} links, each one carries less weight "
                "and the important ones are harder to find.",
        )

    if nofollowed := [link.url for link in internal if link.nofollow]:
        yield notice(
            "links.nofollow_internal",
            f"{len(nofollowed)} internal link(s) marked nofollow",
            evidence=sample(nofollowed),
            fix="Nofollow on internal links wastes authority. Use noindex on the destination "
                "instead if you want to keep it out of the index.",
        )


@analyzer
def link_safety(ctx: PageContext) -> Iterator[Finding]:
    if not ctx.doc:
        return

    if ctx.https:
        insecure = [link.url for link in ctx.doc.links
                    if urlsplit(link.url).scheme == "http"]
        if insecure:
            yield warning(
                "links.insecure",
                f"{len(insecure)} link(s) point to http:// from an https:// page",
                evidence=sample(insecure),
                fix="Update to https://. Many of these will be redirected anyway — the hop "
                    "is wasted crawl budget.",
            )

    unsafe = [link.url for link in ctx.doc.links
              if link.target == "_blank" and "noopener" not in link.rel
              and "noreferrer" not in link.rel and not link.internal]
    if unsafe:
        yield notice(
            "links.unsafe_target_blank",
            f"{len(unsafe)} external link(s) open in a new tab without rel=\"noopener\"",
            evidence=sample(unsafe),
            fix="Add rel=\"noopener\". Current browsers imply it, older ones do not.",
        )
