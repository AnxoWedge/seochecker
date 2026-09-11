"""What changes when JavaScript runs.

Google does render JavaScript, but on a delay and a budget. Bing, most social
preview bots and most AI crawlers largely do not. So content that exists only
after rendering is not invisible — it is slower to be seen, and invisible to a
good part of the web.
"""

from __future__ import annotations

from typing import Iterator

from ..models import Finding
from .base import PageContext, analyzer, notice, warning


@analyzer
def rendering_gap(ctx: PageContext) -> Iterator[Finding]:
    diff = ctx.page.render_diff
    if not diff:
        return

    if error := diff.get("error"):
        yield notice(
            "render.failed",
            "The page could not be rendered, so JavaScript-dependent content was not checked",
            evidence=error,
            fix="Often a timeout on a heavy page. Try --timeout 60.",
        )
        return

    before, after = diff["words_before"], diff["words_after"]
    # Meaningful only when the served HTML was thin to begin with and rendering
    # more than doubled it — small gains are just widgets filling in.
    if before < 100 and after >= max(2 * before, before + 50):
        yield warning(
            "render.content_requires_js",
            f"Most of the content appears only after JavaScript runs "
            f"({before} words served, {after} after rendering)",
            evidence=f"{ctx.page.render_reason}",
            fix="Google renders JavaScript, but on a delay and a budget; Bing, social preview "
                "bots and most AI crawlers largely do not. Server-side rendering or "
                "prerendering puts the content in the first response.",
        )

    links_before, links_after = diff["links_before"], diff["links_after"]
    if links_before == 0 and links_after > 0:
        yield warning(
            "render.links_require_js",
            f"None of this page's {links_after} links exist until JavaScript runs",
            fix="A crawler that does not execute JavaScript finds no way out of this page. "
                "Render the navigation server-side, or provide real <a href> links.",
        )
    elif links_after >= links_before * 2 and links_after - links_before >= 10:
        yield notice(
            "render.links_require_js",
            f"{links_after - links_before} of {links_after} links appear only after rendering",
            fix="Links added by JavaScript are discovered late, if at all. Prefer real "
                "<a href> markup in the served HTML.",
        )

    for field, label in (("title", "title"), ("description", "meta description")):
        was, now = diff.get(f"{field}_before", ""), diff.get(f"{field}_after", "")
        if not was and now:
            yield warning(
                f"render.{field}_requires_js",
                f"The {label} is set by JavaScript, not served in the HTML",
                evidence=f"after rendering: {now[:80]}",
                fix=f"Put the {label} in the served <head>. Anything reading the page without "
                    "executing scripts — including most social and messaging previews — sees "
                    "nothing here.",
            )
