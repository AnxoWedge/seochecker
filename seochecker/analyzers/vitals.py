"""Findings from measured Core Web Vitals.

Not registered as a page analyzer, deliberately: measurement happens after the
crawl, on the pages that matter most by internal PageRank, because each one costs
about ten seconds under throttling. Running it inside the crawl would serialise
the whole thing to measure pages nobody asked about.
"""

from __future__ import annotations

from ..models import Finding, Page
from ..vitals import THRESHOLDS
from .base import notice, warning

LAB_CAVEAT = (
    "Measured in a throttled mobile browser here. Google ranks on field data from "
    "real Chrome users at the 75th percentile, and says lab measurement is not a "
    "substitute for it — so treat this as a diagnosis, not the score itself."
)

ADVICE = {
    "lcp_ms": ("Largest Contentful Paint",
               "The main content takes too long to appear. Usually the hero image or "
               "web font: serve it at the right size, preload it, and make sure the "
               "server responds quickly."),
    "cls": ("Cumulative Layout Shift",
            "Content moves while the page loads. Almost always images or ads without "
            "reserved space — set width and height, or an aspect ratio."),
    "total_blocking_ms": ("Total Blocking Time",
                          "Long JavaScript tasks block the page from responding. This is "
                          "the lab stand-in for Interaction to Next Paint, which cannot "
                          "be measured without real users. Split or defer the heavy work."),
    "fcp_ms": ("First Contentful Paint",
               "Nothing is drawn for too long. Look at render-blocking CSS and "
               "JavaScript in the head."),
    "ttfb_ms": ("Time to First Byte",
                "The server is slow to answer, which delays everything after it."),
}


def findings_for(page: Page) -> list[Finding]:
    """Turn one page's measurements into findings."""
    vitals = page.vitals or {}
    if not vitals or vitals.get("error"):
        return []

    out: list[Finding] = []
    ratings = vitals.get("ratings") or {}
    for metric, rating in ratings.items():
        if rating in ("good", "unknown"):
            continue
        label, advice = ADVICE.get(metric, (metric, ""))
        value = vitals.get(metric)
        good, poor = THRESHOLDS[metric]
        unit = "" if metric == "cls" else "ms"
        shown = f"{value:.3f}" if metric == "cls" else f"{value:.0f}{unit}"
        target = f"{good:.1f}" if metric == "cls" else f"{good:.0f}{unit}"

        # Core Web Vitals proper are warnings when poor; diagnostics stay notices.
        core = metric in ("lcp_ms", "cls")
        make = warning if (core and rating == "poor") else notice
        out.append(make(
            f"vitals.{metric.removesuffix('_ms')}",
            f"{label} is {shown} ({rating.replace('-', ' ')}) — good is under {target}",
            evidence=f"throttled mobile measurement of {page.final_url}",
            fix=f"{advice} {LAB_CAVEAT}",
        ))
        out[-1].url = page.final_url
    return out


def attach(pages: list[Page]) -> int:
    """Add vitals findings to each measured page. Returns how many were added."""
    added = 0
    for page in pages:
        for finding in findings_for(page):
            page.findings.append(finding)
            added += 1
    return added
