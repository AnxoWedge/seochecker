"""Duplicate and near-duplicate content across the crawl.

Only pages a search engine would actually index are compared. A page that is
`noindex`, or that canonicalises to another URL, is a *declared* duplicate —
reporting it would be reporting the solution as the problem.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterator

from ..models import Finding, Page
from ..similarity import similarity
from .base import SiteContext, sample, site_analyzer, warning

# Comparing every page with every other is quadratic, but measured, it costs
# 1.3s at 300 pages and 3.5s at 500 — nothing against a crawl that takes minutes,
# and it cannot miss a pair. So below this size, just compare everything.
FULL_COMPARISON_LIMIT = 600

# Above that, fall back to bucketing pages by the smallest hashes in their
# sketches: two near-identical documents will almost certainly share some. This
# is an approximation — a pair whose shared hashes all land in oversized buckets
# can be missed — which is why it is the fallback rather than the default path.
CANDIDATE_BAND = 32
MAX_BUCKET = 300


def _candidate_pairs(pages: list[Page]) -> set[tuple[str, str]]:
    """Which pairs are worth measuring properly."""
    if len(pages) <= FULL_COMPARISON_LIMIT:
        return {
            tuple(sorted((left.final_url, right.final_url)))
            for i, left in enumerate(pages)
            for right in pages[i + 1:]
        }

    buckets: dict[int, list[Page]] = defaultdict(list)
    for page in pages:
        for value in page.sketch[:CANDIDATE_BAND]:
            buckets[value].append(page)

    candidates: set[tuple[str, str]] = set()
    for bucket in buckets.values():
        if not 1 < len(bucket) <= MAX_BUCKET:
            continue
        for i, left in enumerate(bucket):
            for right in bucket[i + 1:]:
                candidates.add(tuple(sorted((left.final_url, right.final_url))))
    return candidates


def _group_by(pages: list[Page], key: str) -> dict[str, list[Page]]:
    groups: dict[str, list[Page]] = defaultdict(list)
    for page in pages:
        value = (page.seo or {}).get(key) or ""
        if isinstance(value, list):
            value = value[0] if value else ""
        if value := str(value).strip():
            groups[value].append(page)
    return {value: found for value, found in groups.items() if len(found) > 1}


def _describe(groups: dict[str, list[Page]], limit: int = 3) -> str:
    parts = []
    for value, pages in sorted(groups.items(), key=lambda item: -len(item[1]))[:limit]:
        urls = ", ".join(p.final_url for p in pages[:3])
        extra = f" (+{len(pages) - 3} more)" if len(pages) > 3 else ""
        parts.append(f"{value[:50]!r} on {len(pages)} pages: {urls}{extra}")
    return " | ".join(parts)


@site_analyzer
def duplicate_metadata(ctx: SiteContext) -> Iterator[Finding]:
    pages = ctx.indexable_pages
    if len(pages) < 2:
        return

    for key, label, fix in (
        ("title", "titles",
         "Every indexable page needs a distinct title. Identical titles make pages "
         "compete with each other and give searchers nothing to choose between."),
        ("description", "meta descriptions",
         "Write a distinct description per page, or omit it and let Google generate "
         "one from the content — a repeated description helps nobody."),
        ("h1", "H1 headings",
         "The H1 should say what this particular page is about."),
    ):
        if groups := _group_by(pages, key):
            affected = sum(len(found) for found in groups.values())
            yield warning(
                f"duplicate.{key}",
                f"{affected} pages share {len(groups)} duplicated {label}",
                evidence=_describe(groups),
                fix=fix,
            )


@site_analyzer
def duplicate_content(ctx: SiteContext) -> Iterator[Finding]:
    pages = [p for p in ctx.indexable_pages if p.content_hash]
    if len(pages) < 2:
        return

    exact: dict[str, list[Page]] = defaultdict(list)
    for page in pages:
        exact[page.content_hash].append(page)

    duplicated = {fp: found for fp, found in exact.items() if len(found) > 1}
    if duplicated:
        pairs = [f"{found[0].final_url} == {found[1].final_url}"
                 for found in duplicated.values()]
        yield warning(
            "duplicate.content",
            f"{sum(len(f) for f in duplicated.values())} pages have identical body content",
            evidence=sample(pairs, 3),
            fix="Consolidate them, or point the duplicates at one canonical URL. Identical "
                "pages split whatever authority each of them earns.",
        )

    # Near-duplicates: different pages, but not different enough to rank apart.
    # One representative per exact-duplicate group, so a group is not re-reported.
    unique = [found[0] for found in exact.values() if found[0].sketch]
    by_url = {page.final_url: page for page in unique}
    candidates = _candidate_pairs(unique)

    near: list[str] = []
    threshold = ctx.thresholds.near_duplicate_similarity
    for left_url, right_url in sorted(candidates):
        score = similarity(by_url[left_url].sketch, by_url[right_url].sketch)
        if score >= threshold:
            near.append(f"{left_url} ~ {right_url} ({score:.0%} identical)")
    if near:
        yield warning(
            "duplicate.near_content",
            f"{len(near)} pair(s) of pages are near-identical",
            evidence=sample(near, 3),
            fix="Usually paginated archives, filtered listings, or templated pages with only "
                "a name or price changing. Add distinguishing content, or canonicalise them "
                "to one page.",
        )
