"""Open Graph and Twitter cards — how the page looks when someone shares it."""

from __future__ import annotations

from typing import Iterator

from ..models import Finding
from .base import PageContext, analyzer, info, notice, warning

OG_REQUIRED = ("og:title", "og:type", "og:image", "og:url")
OG_RECOMMENDED = ("og:description", "og:site_name")
VALID_TWITTER_CARDS = {"summary", "summary_large_image", "app", "player"}


@analyzer
def open_graph(ctx: PageContext) -> Iterator[Finding]:
    if not ctx.doc:
        return
    present = {tag: ctx.doc.meta_property(tag) for tag in OG_REQUIRED + OG_RECOMMENDED}

    if not any(present.values()):
        yield notice(
            "social.og_missing",
            "No Open Graph tags",
            fix="Add og:title, og:description, og:image and og:url. Without them, shares on "
                "social and messaging apps show whatever the platform guesses.",
        )
        return

    if missing := [tag for tag in OG_REQUIRED if not present[tag]]:
        yield notice(
            "social.og_incomplete",
            f"Open Graph is missing {', '.join(missing)}",
            evidence=", ".join(f"{k}={v[:40]}" for k, v in present.items() if v),
            fix="Fill in the missing properties — og:image especially, since it is what makes "
                "a share visible in a feed.",
        )

    raw_image = ctx.doc.meta_property("og:image")
    if raw_image and not raw_image.lower().startswith(("http://", "https://")):
        yield warning(
            "social.og_image_relative",
            "og:image is a relative URL",
            evidence=raw_image,
            fix="The Open Graph spec requires an absolute URL. Most platforms will not resolve "
                "a relative one, so the preview shows no image.",
        )

    if (og_url := present["og:url"]) and og_url.rstrip("/") != ctx.url.rstrip("/"):
        yield notice(
            "social.og_url_mismatch",
            "og:url does not match this page's URL",
            evidence=f"page: {ctx.url} -> og:url: {og_url}",
            fix="og:url should be this page's canonical URL — it is how platforms deduplicate "
                "share counts.",
        )


@analyzer
def twitter_card(ctx: PageContext) -> Iterator[Finding]:
    if not ctx.doc:
        return
    card = ctx.doc.meta("twitter:card")

    if not card:
        # Falls back to Open Graph, so this only matters when OG is missing too.
        if not ctx.doc.meta_property("og:image"):
            yield info(
                "social.twitter_missing",
                "No twitter:card and no og:image to fall back on",
                fix="Add twitter:card=\"summary_large_image\" plus an image.",
            )
        return

    if card not in VALID_TWITTER_CARDS:
        yield notice(
            "social.twitter_card_invalid",
            f"twitter:card value {card!r} is not a recognised card type",
            evidence=f"expected one of: {', '.join(sorted(VALID_TWITTER_CARDS))}",
            fix="Use summary_large_image for most content pages.",
        )
