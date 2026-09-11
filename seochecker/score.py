"""Scoring.

A score is a heuristic, so this one is built to be argued with rather than
trusted blindly: every deduction is recorded with the finding that caused it and
how much of the site it affected, and the report shows that working.

The shape is: each category starts at 100, and every distinct finding deducts
according to its severity, scaled by the share of pages it affects. A critical
issue on every page costs 40 points; the same issue on one page in a hundred
costs 0.4.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .models import Finding, Severity

# Deductions from a *category's* score. A single critical affecting the whole
# site takes its category to 15, which is what "this area is broken" should read
# like.
CATEGORY_SEVERITY = {
    Severity.CRITICAL: 85.0,
    Severity.WARNING: 30.0,
    Severity.NOTICE: 10.0,
    Severity.INFO: 0.0,
}

# Deductions from the *overall* score, multiplied by the category's weight.
#
# The overall is computed from findings directly rather than by averaging the
# category scores. Averaging was the first attempt and it was badly wrong: with
# eighteen categories, a site with every page set to noindex — invisible to
# search — scored 94.8 out of 100, because seventeen clean categories drowned the
# one catastrophic one.
#
# These three numbers were fitted against a table of stated targets rather than
# picked: a perfect site scores 100, one critical on one page in a hundred barely
# registers, and a site-wide critical in a heavily weighted category reaches 0.
OVERALL_SEVERITY = {
    Severity.CRITICAL: 45.0,
    Severity.WARNING: 15.0,
    Severity.NOTICE: 1.0,
    Severity.INFO: 0.0,
}

# How much each area counts toward the overall number. Indexability is weighted
# highest because a noindex tag makes every other score irrelevant.
CATEGORY_WEIGHT = {
    "indexability": 3.0,
    "technical": 2.5,
    "canonical": 2.0,
    "title": 2.0,
    "content": 2.0,
    "structure": 1.5,
    "duplicate": 1.5,
    "description": 1.0,
    "headings": 1.0,
    "images": 1.0,
    "links": 1.0,
    "sitemap": 1.0,
    "robots": 1.0,
    "render": 1.0,
    "url": 0.5,
    "i18n": 0.5,
    "social": 0.5,
    "structured": 0.5,
    "crawl": 0.5,
    "external": 0.5,
}

# Evaluated on every run. `external` and `render` are added only when those
# passes actually ran — scoring a check that never executed as a clean 100 would
# quietly inflate the result.
ALWAYS_EVALUATED = frozenset({
    "title", "description", "indexability", "canonical", "headings", "images",
    "links", "social", "structured", "i18n", "technical", "content", "url",
})
CRAWL_EVALUATED = frozenset({"sitemap", "robots", "structure", "duplicate", "crawl"})

GRADES = ((90, "A"), (80, "B"), (70, "C"), (60, "D"), (50, "E"), (0, "F"))


@dataclass(slots=True)
class Deduction:
    finding_id: str
    severity: str
    points: float           # cost to this category's score
    overall_points: float   # cost to the overall score
    affected_pages: int
    share: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "severity": self.severity,
            "points": round(self.points, 2),
            "overall_points": round(self.overall_points, 2),
            "affected_pages": self.affected_pages,
            "share_of_site": round(self.share, 4),
        }


@dataclass(slots=True)
class CategoryScore:
    category: str
    score: float
    weight: float
    deductions: list[Deduction] = field(default_factory=list)

    @property
    def issue_count(self) -> int:
        return len(self.deductions)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "score": round(self.score, 1),
            "weight": self.weight,
            "deductions": [d.to_dict() for d in self.deductions],
        }


@dataclass(slots=True)
class Scorecard:
    overall: float
    grade: str
    categories: list[CategoryScore] = field(default_factory=list)
    pages: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall": round(self.overall, 1),
            "grade": self.grade,
            "pages_scored": self.pages,
            "categories": [c.to_dict() for c in self.categories],
        }


def grade_for(score: float) -> str:
    for floor, letter in GRADES:
        if score >= floor:
            return letter
    return "F"


def evaluated_categories(*, crawled: bool, rendered: bool, external: bool) -> set[str]:
    categories = set(ALWAYS_EVALUATED)
    if crawled:
        categories |= CRAWL_EVALUATED
    if rendered:
        categories.add("render")
    if external:
        categories.add("external")
    return categories


def score(findings: list[Finding], *, pages: int, categories: set[str]) -> Scorecard:
    """Turn findings into a scorecard. `pages` is the crawl size, minimum 1."""
    pages = max(1, pages)

    # One deduction per distinct finding id, sized by how much of the site it hits.
    affected: dict[tuple[str, str], set[str]] = defaultdict(set)
    scope_of: dict[str, int] = {}
    severity_of: dict[str, Severity] = {}
    for finding in findings:
        if finding.category not in categories:
            continue
        affected[(finding.category, finding.id)].add(finding.url or "__site__")
        if finding.affected:
            scope_of[finding.id] = max(scope_of.get(finding.id, 0), finding.affected)
        # If one finding id appears at more than one severity, the worst wins.
        current = severity_of.get(finding.id)
        if current is None or CATEGORY_SEVERITY[finding.severity] > CATEGORY_SEVERITY[current]:
            severity_of[finding.id] = finding.severity

    by_category: dict[str, list[Deduction]] = defaultdict(list)
    for (category, finding_id), urls in affected.items():
        severity = severity_of[finding_id]
        if not CATEGORY_SEVERITY[severity]:
            continue
        site_level = urls == {"__site__"}
        if site_level:
            # A site-level finding that says how many pages it concerns is scored
            # on that; one that does not is treated as concerning the whole site.
            count = min(scope_of.get(finding_id, pages), pages)
            share = count / pages
        else:
            count = len(urls)
            share = min(1.0, count / pages)
        by_category[category].append(
            Deduction(
                finding_id=finding_id,
                severity=severity.value,
                points=CATEGORY_SEVERITY[severity] * share,
                overall_points=(OVERALL_SEVERITY[severity] * share
                                * CATEGORY_WEIGHT.get(category, 1.0)),
                affected_pages=count,
                share=share,
            )
        )

    scored: list[CategoryScore] = []
    for category in sorted(categories):
        deductions = sorted(by_category.get(category, []), key=lambda d: -d.points)
        total = sum(d.points for d in deductions)
        scored.append(
            CategoryScore(
                category=category,
                score=max(0.0, 100.0 - total),
                weight=CATEGORY_WEIGHT.get(category, 1.0),
                deductions=deductions,
            )
        )

    overall = max(0.0, 100.0 - sum(d.overall_points
                                   for deductions in by_category.values()
                                   for d in deductions))
    scored.sort(key=lambda c: (c.score, -c.weight))
    return Scorecard(overall=overall, grade=grade_for(overall), categories=scored, pages=pages)
