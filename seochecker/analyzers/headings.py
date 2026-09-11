"""Heading structure — the document outline a crawler and a screen reader both read."""

from __future__ import annotations

from typing import Iterator

from ..models import Finding
from .base import PageContext, analyzer, notice, sample, warning


@analyzer
def headings(ctx: PageContext) -> Iterator[Finding]:
    if not ctx.doc:
        return
    outline = ctx.doc.headings

    if not outline:
        yield warning(
            "headings.none",
            "Page has no headings at all",
            fix="Structure the content with an <h1> and supporting <h2>s. Headings are how "
                "both crawlers and assistive technology navigate a page.",
        )
        return

    h1s = ctx.doc.h1s
    if not h1s:
        yield warning(
            "headings.no_h1",
            "No <h1> on the page",
            evidence=f"first heading is h{outline[0][0]}: {outline[0][1][:60]}",
            fix="Add one <h1> stating what this page is about.",
        )
    elif len(h1s) > 1:
        yield notice(
            "headings.multiple_h1",
            f"{len(h1s)} <h1> headings",
            evidence=sample(h1s, 3),
            fix="Valid in HTML5 and no longer penalised, but a single <h1> makes the page's "
                "subject unambiguous.",
        )

    if empty := [f"h{level}" for level, text in outline if not text]:
        yield notice(
            "headings.empty",
            f"{len(empty)} empty heading tag(s)",
            evidence=sample(empty),
            fix="Remove them, or fill them in. Empty headings are usually leftover styling hooks.",
        )

    skips: list[str] = []
    previous = 0
    for level, text in outline:
        if previous and level > previous + 1:
            skips.append(f"h{previous} -> h{level} ({text[:40] or 'empty'})")
        previous = level
    if skips:
        yield notice(
            "headings.skipped_level",
            f"Heading levels skip a rank in {len(skips)} place(s)",
            evidence=sample(skips, 3),
            fix="Go down one level at a time (h2 then h3, not h2 then h4) so the outline "
                "stays coherent.",
        )
