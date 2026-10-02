"""What each finding is actually for.

Three questions a site owner asks, and one finding often answers more than one:

- **SEO** — will search engines index this, understand it, and rank it?
- **AIO** — will AI assistants be able to read it and cite it? Mostly the same
  work as SEO, with two differences that matter: answer engines are separate
  crawlers from training crawlers, and almost none of them execute JavaScript.
- **Performance** — how fast is it for the person who actually arrives?

Tagging findings this way is not decoration. "You have 154 findings" is not
useful; "these six things cost you AI citations" is.
"""

from __future__ import annotations

SEO = "seo"
AIO = "aio"
PERFORMANCE = "performance"

ALL_LENSES = (SEO, AIO, PERFORMANCE)

LABELS = {
    SEO: "Search",
    AIO: "AI answers",
    PERFORMANCE: "Speed",
}

# Whole categories that belong to one or more lenses.
CATEGORY_LENSES: dict[str, tuple[str, ...]] = {
    "title": (SEO, AIO),
    "description": (SEO,),
    "indexability": (SEO, AIO),
    "canonical": (SEO,),
    "headings": (SEO, AIO),
    "images": (SEO,),
    "links": (SEO, AIO),
    "social": (SEO,),
    "structured": (SEO, AIO),
    "i18n": (SEO,),
    "content": (SEO, AIO),
    "url": (SEO,),
    "technical": (SEO,),
    "sitemap": (SEO, AIO),
    "robots": (SEO, AIO),
    "duplicate": (SEO,),
    "structure": (SEO, AIO),
    "crawl": (SEO,),
    "external": (SEO,),
    "render": (SEO, AIO),
    "vitals": (PERFORMANCE,),
}

# Individual findings whose lenses differ from their category's.
FINDING_LENSES: dict[str, tuple[str, ...]] = {
    # Delivery is felt by the visitor before it is felt by a ranking.
    "technical.ttfb_slow": (PERFORMANCE, SEO),
    "technical.ttfb_very_slow": (PERFORMANCE, SEO),
    "technical.no_compression": (PERFORMANCE,),
    "technical.large_html": (PERFORMANCE,),
    "technical.http1": (PERFORMANCE,),
    "technical.render_blocking_scripts": (PERFORMANCE,),
    "technical.no_cache_validators": (PERFORMANCE,),
    "images.missing_dimensions": (PERFORMANCE,),
    "images.no_lazy_loading": (PERFORMANCE,),
    # An AI crawler that does not render sees nothing here at all.
    "content.empty": (AIO, SEO),
    "content.js_rendered": (AIO, SEO),
    "render.content_requires_js": (AIO, SEO),
    "render.links_require_js": (AIO, SEO),
    "render.some_links_require_js": (AIO,),
    "render.title_requires_js": (AIO, SEO),
    "render.description_requires_js": (AIO, SEO),
    # Answer engines, specifically.
    "robots.blocks_answer_engines": (AIO,),
    "robots.blocks_ai_training": (AIO,),
    "robots.no_llms_txt": (AIO,),
}


def lenses_for(finding_id: str, category: str) -> tuple[str, ...]:
    """Every lens a finding bears on. Used for scoring, where double-counting is
    correct: a page that needs JavaScript genuinely costs both search and AI."""
    if finding_id in FINDING_LENSES:
        return FINDING_LENSES[finding_id]
    return CATEGORY_LENSES.get(category, (SEO,))


def primary_lens(finding_id: str, category: str) -> str:
    """The one lens a finding is listed under.

    Scoring counts a finding against every lens it affects; the report shows it
    once. Listing the same thing under two headings is the kind of duplication
    that makes a report feel long without saying more.
    """
    return lenses_for(finding_id, category)[0]
