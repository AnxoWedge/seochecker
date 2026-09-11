"""Side-by-side comparison against rival sites.

What this can and cannot say matters enough to state at the top of the module. A
crawl sees what is on the pages: markup, structure, technology, delivery. It does
not see backlinks, traffic, or rankings. So this answers "what are they doing on
their pages that we are not", and it must never be read as "who ranks better".

Every site is crawled to the same page budget, because a comparison of 200 pages
against 12 is not a comparison. The budget is carried in the output so the reader
can see it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from statistics import median
from typing import Any
from urllib.parse import urlsplit

from .crawl import CrawlResult
from .models import Page, Severity
from .score import Scorecard

# A category score has to differ by more than this before it is worth calling a
# gap; below it, the difference is one notice on one page.
MEANINGFUL_GAP = 5.0
THIN_WORDS = 300

DISCLAIMER = (
    "This compares what is visible on the pages themselves — markup, structure, "
    "technology and delivery. A crawl cannot see backlinks, traffic or rankings, "
    "so nothing here says who ranks better."
)


def _median(values: list[float]) -> float:
    return round(median(values), 1) if values else 0.0


def _share(count: int, total: int) -> float:
    return round(count / total, 3) if total else 0.0


@dataclass
class SiteMetrics:
    """Everything comparable about one crawled site."""

    url: str
    host: str
    budget: int
    pages_crawled: int = 0
    html_pages: int = 0
    score: float = 0.0
    grade: str = ""
    category_scores: dict[str, float] = field(default_factory=dict)
    findings: dict[str, int] = field(default_factory=dict)

    technologies: dict[str, list[str]] = field(default_factory=dict)
    schema_types: list[str] = field(default_factory=list)

    median_words: float = 0.0
    thin_share: float = 0.0
    missing_title_share: float = 0.0
    missing_description_share: float = 0.0
    median_title_length: float = 0.0
    median_description_length: float = 0.0
    structured_data_share: float = 0.0

    max_click_depth: int = 0
    median_inlinks: float = 0.0
    links_per_page: float = 0.0
    orphan_share: float = 0.0

    https_share: float = 0.0
    compression_share: float = 0.0
    http2_share: float = 0.0
    median_ttfb_ms: float = 0.0
    median_html_kb: float = 0.0

    has_sitemap: bool = False
    sitemap_urls: int = 0
    robots_found: bool = False

    @classmethod
    def from_result(cls, result: CrawlResult, card: Scorecard, budget: int) -> "SiteMetrics":
        target = result.pages[0].requested_url if result.pages else ""
        metrics = cls(
            url=target,
            host=urlsplit(target).netloc,
            budget=budget,
            pages_crawled=len(result.pages),
            score=round(card.overall, 1),
            grade=card.grade,
            category_scores={c.category: round(c.score, 1) for c in card.categories},
        )

        severity = {level.value: 0 for level in Severity}
        for finding in list(result.site_findings) + [f for p in result.pages for f in p.findings]:
            severity[finding.severity.value] += 1
        metrics.findings = severity

        by_category: dict[str, list[str]] = defaultdict(list)
        for tech in result.technologies:
            by_category[tech.category].append(tech.name)
        metrics.technologies = {k: sorted(v) for k, v in sorted(by_category.items())}

        html = [p for p in result.pages if p.error is None and p.ok and p.seo]
        metrics.html_pages = len(html)
        if html:
            metrics._fill_content(html)
            metrics._fill_structure(result, html)
            metrics._fill_delivery(html)

        metrics.robots_found = result.robots.fetched
        metrics.has_sitemap = bool(result.sitemap.fetched)
        metrics.sitemap_urls = len(result.sitemap.entries)
        return metrics

    # --- the three groups of measurements ----------------------------------

    def _fill_content(self, html: list[Page]) -> None:
        words = [p.seo.get("word_count", 0) for p in html]
        titles = [p.seo.get("title", "") for p in html]
        descriptions = [p.seo.get("description", "") for p in html]
        schema: set[str] = set()
        with_schema = 0
        for page in html:
            found = page.seo.get("json_ld_types") or []
            schema.update(found)
            if found:
                with_schema += 1

        self.median_words = _median(words)
        self.thin_share = _share(sum(1 for w in words if w < THIN_WORDS), len(html))
        self.missing_title_share = _share(sum(1 for t in titles if not t), len(html))
        self.missing_description_share = _share(
            sum(1 for d in descriptions if not d), len(html))
        self.median_title_length = _median([len(t) for t in titles if t])
        self.median_description_length = _median([len(d) for d in descriptions if d])
        self.schema_types = sorted(schema)
        self.structured_data_share = _share(with_schema, len(html))

    def _fill_structure(self, result: CrawlResult, html: list[Page]) -> None:
        graph = result.graph
        self.max_click_depth = max(graph.click_depth.values(), default=0)
        self.median_inlinks = _median([p.inlink_count for p in html])
        outlinks = sum(len(p.outlinks) for p in html)
        self.links_per_page = round(outlinks / len(html), 1) if html else 0.0
        self.orphan_share = _share(len(graph.orphans()), len(graph.nodes) or 1)

    def _fill_delivery(self, html: list[Page]) -> None:
        total = len(html)
        self.https_share = _share(
            sum(1 for p in html if urlsplit(p.final_url).scheme == "https"), total)
        self.compression_share = _share(
            sum(1 for p in html if p.header("content-encoding")), total)
        self.http2_share = _share(
            sum(1 for p in html if p.http_version.upper() not in ("HTTP/1.0", "HTTP/1.1")), total)
        self.median_ttfb_ms = _median(
            [p.timing.ttfb_ms for p in html if p.timing.ttfb_ms is not None])
        self.median_html_kb = _median([p.decoded_bytes / 1024 for p in html if p.decoded_bytes])

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items()}


@dataclass
class Gap:
    """One thing a rival does that the target does not."""

    kind: str                 # technology | schema | category | metric
    label: str
    detail: str = ""
    rivals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "label": self.label, "detail": self.detail,
                "rivals": self.rivals}


# Metrics worth comparing directly, and which direction is better.
METRIC_ROWS: list[tuple[str, str, str, bool]] = [
    # (attribute, label, format, higher_is_better)
    ("pages_crawled", "Pages crawled", "int", True),
    ("median_words", "Median words per page", "num", True),
    ("thin_share", "Pages under 300 words", "pct", False),
    ("structured_data_share", "Pages with structured data", "pct", True),
    ("missing_title_share", "Pages with no title", "pct", False),
    ("missing_description_share", "Pages with no description", "pct", False),
    ("median_title_length", "Median title length", "num", True),
    ("links_per_page", "Internal links per page", "num", True),
    ("median_inlinks", "Median inbound internal links", "num", True),
    ("max_click_depth", "Deepest page, in clicks", "int", False),
    ("orphan_share", "Orphan pages", "pct", False),
    ("https_share", "Served over HTTPS", "pct", True),
    ("compression_share", "Compressed responses", "pct", True),
    ("http2_share", "HTTP/2 or better", "pct", True),
    ("median_ttfb_ms", "Median server response, ms", "num", False),
    ("median_html_kb", "Median HTML size, KB", "num", False),
]


def format_metric(value: Any, kind: str) -> str:
    if kind == "pct":
        return f"{value * 100:.0f}%"
    if kind == "int":
        return f"{int(value)}"
    return f"{value:g}"


@dataclass
class Comparison:
    target: SiteMetrics
    rivals: list[SiteMetrics]
    budget: int
    disclaimer: str = DISCLAIMER

    @property
    def sites(self) -> list[SiteMetrics]:
        return [self.target, *self.rivals]

    # --- gaps --------------------------------------------------------------

    def technology_gaps(self) -> list[Gap]:
        mine = {name for names in self.target.technologies.values() for name in names}
        found: dict[tuple[str, str], list[str]] = defaultdict(list)
        for rival in self.rivals:
            for category, names in rival.technologies.items():
                for name in names:
                    if name not in mine:
                        found[(category, name)].append(rival.host)
        return [Gap(kind="technology", label=name, detail=category, rivals=hosts)
                for (category, name), hosts in sorted(found.items())]

    def schema_gaps(self) -> list[Gap]:
        mine = set(self.target.schema_types)
        found: dict[str, list[str]] = defaultdict(list)
        for rival in self.rivals:
            for schema in rival.schema_types:
                if schema not in mine:
                    found[schema].append(rival.host)
        return [Gap(kind="schema", label=schema, rivals=hosts)
                for schema, hosts in sorted(found.items())]

    def category_gaps(self) -> list[Gap]:
        """Areas where a rival scores meaningfully better than the target."""
        gaps: list[Gap] = []
        for category, mine in sorted(self.target.category_scores.items()):
            best_host, best_score = "", mine
            for rival in self.rivals:
                theirs = rival.category_scores.get(category)
                if theirs is not None and theirs > best_score + MEANINGFUL_GAP:
                    best_host, best_score = rival.host, theirs
            if best_host:
                gaps.append(Gap(kind="category", label=category,
                                detail=f"{mine:.0f} vs {best_score:.0f}",
                                rivals=[best_host]))
        return sorted(gaps, key=lambda g: -(float(g.detail.split(" vs ")[1])
                                            - float(g.detail.split(" vs ")[0])))

    def strengths(self) -> list[Gap]:
        """Areas where the target is meaningfully ahead of every rival."""
        out: list[Gap] = []
        for category, mine in sorted(self.target.category_scores.items()):
            theirs = [r.category_scores.get(category) for r in self.rivals]
            theirs = [t for t in theirs if t is not None]
            if theirs and mine > max(theirs) + MEANINGFUL_GAP:
                out.append(Gap(kind="category", label=category,
                               detail=f"{mine:.0f} vs {max(theirs):.0f} best rival"))
        return out

    def metric_table(self) -> list[dict[str, Any]]:
        rows = []
        for attribute, label, kind, higher_better in METRIC_ROWS:
            values = [getattr(site, attribute) for site in self.sites]
            best = max(values) if higher_better else min(values)
            rows.append({
                "label": label,
                "kind": kind,
                "cells": [
                    {"raw": value, "text": format_metric(value, kind),
                     "best": value == best and len(set(values)) > 1}
                    for value in values
                ],
            })
        return rows

    def to_dict(self) -> dict[str, Any]:
        return {
            "budget": self.budget,
            "disclaimer": self.disclaimer,
            "target": self.target.to_dict(),
            "rivals": [r.to_dict() for r in self.rivals],
            "gaps": {
                "technology": [g.to_dict() for g in self.technology_gaps()],
                "schema": [g.to_dict() for g in self.schema_gaps()],
                "categories": [g.to_dict() for g in self.category_gaps()],
            },
            "strengths": [g.to_dict() for g in self.strengths()],
        }
