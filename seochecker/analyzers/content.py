"""Content volume and whether it survives without JavaScript."""

from __future__ import annotations

from typing import Iterator

from ..models import Finding
from .base import PageContext, analyzer, critical, info, notice, warning


@analyzer
def content_volume(ctx: PageContext) -> Iterator[Finding]:
    if not ctx.doc:
        return
    words = ctx.doc.word_count
    limits = ctx.thresholds

    if words == 0:
        scripts = len(ctx.doc.scripts)
        yield critical(
            "content.empty",
            "No readable text in the served HTML",
            evidence=f"{scripts} script tag(s), {len(ctx.doc.raw)} bytes of markup",
            fix="If this is a JavaScript-rendered page, crawlers that do not execute JS see "
                "nothing here. Re-run with rendering (Phase 6) to confirm, and consider "
                "server-side rendering or prerendering.",
        )
        return

    if words < limits.very_thin_content:
        yield warning(
            "content.very_thin",
            f"Only {words} words of text",
            fix=f"Pages under ~{limits.very_thin_content} words rarely have enough substance "
                "to rank for anything competitive.",
        )
    elif words < limits.thin_content:
        yield notice(
            "content.thin",
            f"{words} words of text",
            fix=f"Thin relative to a typical ranking page (~{limits.thin_content}+ words), "
                "though short pages are fine when they answer a narrow question.",
        )

    html_bytes = len(ctx.doc.raw.encode("utf-8", "ignore"))
    if html_bytes:
        ratio = len(ctx.doc.text) / html_bytes
        if ratio < limits.text_ratio_min:
            yield notice(
                "content.low_text_ratio",
                f"Text is only {ratio:.1%} of the HTML",
                evidence=f"{len(ctx.doc.text):,} characters of text in {html_bytes:,} bytes of markup",
                fix="Mostly a symptom: heavy inline scripts, styles or generated markup. Worth "
                    "checking the page is not shipping far more code than content.",
            )


@analyzer
def javascript_dependence(ctx: PageContext) -> Iterator[Finding]:
    """Flag pages whose served HTML looks like an empty app shell."""
    if not ctx.doc or ctx.doc.word_count == 0:
        return  # content.empty already covers the total case
    if ctx.doc.word_count >= ctx.thresholds.very_thin_content:
        return

    shell_markers = [
        node for node in ctx.doc.tree.css("div[id]")
        if (node.attributes.get("id") or "").lower() in ("root", "app", "__next", "___gatsby")
    ]
    if shell_markers and len(ctx.doc.scripts) >= 3:
        yield info(
            "content.js_rendered",
            "Looks like a JavaScript-rendered app shell",
            evidence=f"#{shell_markers[0].attributes.get('id')} with {len(ctx.doc.scripts)} scripts "
                     f"and {ctx.doc.word_count} words in the served HTML",
            fix="What a crawler sees before running JS is what you see here. Phase 6 rendering "
                "will show the difference.",
        )
