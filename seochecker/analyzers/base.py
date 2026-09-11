"""Analyzer plumbing: the context they receive, and the registry they join.

An analyzer is a plain function that takes a `PageContext` and yields `Finding`s.
No base class, no inheritance — adding a check means writing a function and
decorating it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Iterator
from urllib.parse import urlsplit

from ..config import CrawlConfig
from ..html import Document
from ..models import Finding, Page, Severity
from ..robots import RobotsTxt
from ..sitemap import SitemapSet
from ..thresholds import Thresholds

Analyzer = Callable[["PageContext"], Iterable[Finding]]
SiteAnalyzer = Callable[["SiteContext"], Iterable[Finding]]

REGISTRY: list[Analyzer] = []

# Analyzers safe to run when the fetch failed or was challenged. Everything else
# would be describing a bot-mitigation page as if it were the site.
ERROR_SAFE: set[str] = set()

# Checks that need the whole crawl, not one page.
SITE_REGISTRY: list["SiteAnalyzer"] = []

# Most severe first, for sorting output.
SEVERITY_ORDER = {
    Severity.CRITICAL: 0,
    Severity.WARNING: 1,
    Severity.NOTICE: 2,
    Severity.INFO: 3,
}


def analyzer(fn: Analyzer | None = None, *, when_errored: bool = False):
    """Register a page analyzer.

    `when_errored=True` marks a check that still makes sense when the fetch
    failed — reporting the failure itself, essentially.
    """

    def register(func: Analyzer) -> Analyzer:
        REGISTRY.append(func)
        if when_errored:
            ERROR_SAFE.add(f"{func.__module__}.{func.__name__}")
        return func

    return register(fn) if fn is not None else register


@dataclass(slots=True)
class SiteContext:
    """Everything a whole-crawl check needs."""

    config: CrawlConfig
    pages: list[Page] = field(default_factory=list)
    robots: RobotsTxt = field(default_factory=RobotsTxt)
    sitemap: SitemapSet = field(default_factory=SitemapSet)
    sitemap_urls: set[str] = field(default_factory=set)
    frontier: dict = field(default_factory=dict)
    technologies: list = field(default_factory=list)
    thresholds: Thresholds = field(default_factory=Thresholds)

    @property
    def html_pages(self) -> list[Page]:
        return [p for p in self.pages if p.error is None and p.is_html]

    def page_for(self, url: str) -> Page | None:
        from ..urls import normalize
        target = normalize(url)
        for page in self.pages:
            if normalize(page.requested_url) == target:
                return page
        return None


@dataclass(slots=True)
class PageContext:
    page: Page
    doc: Document | None
    config: CrawlConfig
    thresholds: Thresholds
    technologies: list = field(default_factory=list)  # list[Detection]

    @property
    def url(self) -> str:
        return self.page.final_url

    @property
    def https(self) -> bool:
        return urlsplit(self.url).scheme == "https"

    @property
    def host(self) -> str:
        return urlsplit(self.url).netloc.lower()

    @property
    def has_html(self) -> bool:
        return self.doc is not None


# --- Finding constructors ---------------------------------------------------
# `id` is stable and machine-readable (`title.too_long`); `message` is what a
# human reads; `fix` is what they should do about it.

def _make(severity: Severity, id: str, message: str, evidence: str, fix: str) -> Finding:
    return Finding(
        id=id,
        severity=severity,
        category=id.split(".", 1)[0],
        message=message,
        evidence=evidence,
        fix=fix,
    )


def critical(id: str, message: str, *, evidence: str = "", fix: str = "") -> Finding:
    return _make(Severity.CRITICAL, id, message, evidence, fix)


def warning(id: str, message: str, *, evidence: str = "", fix: str = "") -> Finding:
    return _make(Severity.WARNING, id, message, evidence, fix)


def notice(id: str, message: str, *, evidence: str = "", fix: str = "") -> Finding:
    return _make(Severity.NOTICE, id, message, evidence, fix)


def info(id: str, message: str, *, evidence: str = "", fix: str = "") -> Finding:
    return _make(Severity.INFO, id, message, evidence, fix)


def site_analyzer(fn: "SiteAnalyzer") -> "SiteAnalyzer":
    """Register a whole-crawl check."""
    SITE_REGISTRY.append(fn)
    return fn


def run_site_analyzers(ctx: SiteContext) -> list[Finding]:
    findings: list[Finding] = []
    for fn in SITE_REGISTRY:
        try:
            findings.extend(fn(ctx))
        except Exception as exc:  # noqa: BLE001
            findings.append(
                Finding(id="analyzer.error", severity=Severity.INFO, category="analyzer",
                        message=f"site analyzer {fn.__name__!r} failed",
                        evidence=f"{type(exc).__name__}: {exc}")
            )
    findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.category, f.id))
    return findings


def sample(items: Iterable[str], limit: int = 5) -> str:
    """Render a few examples plus a count, for evidence strings."""
    items = list(items)
    shown = ", ".join(items[:limit])
    extra = len(items) - limit
    return f"{shown}{f' (+{extra} more)' if extra > 0 else ''}"


def run_page_analyzers(ctx: PageContext) -> list[Finding]:
    """Run every registered analyzer. One bad analyzer must not sink the page."""
    findings: list[Finding] = []
    active = (
        REGISTRY if ctx.page.error is None
        else [fn for fn in REGISTRY if f"{fn.__module__}.{fn.__name__}" in ERROR_SAFE]
    )
    for fn in active:
        try:
            for finding in fn(ctx):
                finding.url = ctx.url
                findings.append(finding)
        except Exception as exc:  # noqa: BLE001 — a broken check is a bug, not a crash
            findings.append(
                Finding(
                    id="analyzer.error",
                    severity=Severity.INFO,
                    category="analyzer",
                    message=f"analyzer {fn.__name__!r} failed",
                    evidence=f"{type(exc).__name__}: {exc}",
                    url=ctx.url,
                )
            )
    findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.category, f.id))
    return findings


def parse_directives(values: Iterable[str]) -> set[str]:
    """Normalise robots directives from meta tags and X-Robots-Tag headers.

    Handles the optional per-bot prefix (`googlebot: noindex, nofollow`).
    """
    out: set[str] = set()
    for value in values:
        value = value.strip()
        # A bot prefix, if present, is on the whole value: `googlebot: noindex, nofollow`.
        # It must be stripped before splitting, or `max-image-preview:large`
        # (a directive that also contains a colon) gets mangled.
        head, sep, tail = value.partition(":")
        if sep and head.strip().lower() in _BOT_NAMES:
            value = tail
        for part in value.split(","):
            if token := part.strip().lower():
                out.add(token)
    return out


_BOT_NAMES = frozenset({
    "googlebot", "googlebot-news", "bingbot", "google", "otherbot", "robots",
    "slurp", "duckduckbot", "yandex", "baiduspider", "applebot",
})
