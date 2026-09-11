"""Data model shared by the fetch layer and everything downstream.

The analyzers never see an httpx object: they see a `Page`. That keeps the HTTP
client swappable (Phase 6 swaps in a rendered-DOM fetcher behind the same shape).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class ErrorKind(str, Enum):
    """Why a fetch produced no usable response."""

    TIMEOUT = "timeout"
    CONNECTION = "connection"
    TLS = "tls"
    TOO_MANY_REDIRECTS = "too_many_redirects"
    REDIRECT_LOOP = "redirect_loop"
    INVALID_URL = "invalid_url"
    TOO_LARGE = "too_large"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class Severity(str, Enum):
    CRITICAL = "critical"
    WARNING = "warning"
    NOTICE = "notice"
    INFO = "info"


@dataclass(slots=True)
class Finding:
    """One SEO issue (or observation) about one page or about the site."""

    id: str
    severity: Severity
    category: str
    message: str
    evidence: str = ""
    fix: str = ""
    url: str | None = None
    # For site-level findings: how many pages this actually concerns. 0 means the
    # whole site. Without it, "one pair of pages is near-identical" would be
    # scored as a problem affecting every page.
    affected: int = 0


@dataclass(slots=True)
class RedirectHop:
    url: str
    status: int
    location: str
    elapsed_ms: float


@dataclass(slots=True)
class Timing:
    """Milliseconds. `ttfb` is time to response headers, not to first content byte."""

    ttfb_ms: float | None = None
    download_ms: float | None = None
    total_ms: float | None = None
    connect_ms: float | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(slots=True)
class Page:
    """Everything one HTTP fetch produced, successful or not."""

    requested_url: str
    final_url: str = ""
    status: int | None = None
    reason: str = ""
    http_version: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)  # from Set-Cookie
    content_type: str = ""
    mime: str = ""
    charset: str = ""
    charset_source: str = ""
    wire_bytes: int = 0
    decoded_bytes: int = 0
    truncated: bool = False
    html: str | None = None
    redirects: list[RedirectHop] = field(default_factory=list)
    timing: Timing = field(default_factory=Timing)
    requests: int = 0     # HTTP exchanges issued: redirect hops + retries
    retries: int = 0      # of those, how many were repeat attempts
    fetched_at: str = field(default_factory=_now)
    error: ErrorKind | None = None
    error_detail: str = ""
    depth: int = 0
    referrer: str | None = None
    from_cache: bool = False
    from_sitemap: bool = False   # discovered via sitemap, not by a link
    duplicate_of: str = ""       # redirected onto a URL already crawled
    vitals: dict[str, Any] = field(default_factory=dict)   # lab Core Web Vitals

    # Captured during the crawl so whole-site analysis has something to work
    # with once the parsed document is gone.
    seo: dict[str, Any] = field(default_factory=dict)
    outlinks: list[str] = field(default_factory=list)        # normalized, in scope
    external_links: list[str] = field(default_factory=list)
    content_hash: str = ""                 # exact fingerprint of the visible text
    sketch: tuple[int, ...] = ()           # MinHash sketch, for near-duplicates

    # Filled in after the crawl, from the link graph.
    pagerank: float = 0.0
    click_depth: int | None = None
    inlink_count: int = 0
    rendered: bool = False
    render_reason: str = ""
    js_globals: list[str] = field(default_factory=list)
    render_diff: dict[str, Any] = field(default_factory=dict)
    findings: list[Finding] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.final_url:
            self.final_url = self.requested_url

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and 200 <= self.status < 300

    @property
    def is_html(self) -> bool:
        return self.mime in ("text/html", "application/xhtml+xml")

    @property
    def redirected(self) -> bool:
        return bool(self.redirects)

    def header(self, name: str, default: str = "") -> str:
        """Case-insensitive header lookup."""
        return self.headers.get(name.lower(), default)

    def to_dict(self, *, include_html: bool = False,
                include_links: bool = False) -> dict[str, Any]:
        data = asdict(self)
        data["error"] = self.error.value if self.error else None
        data["findings"] = [
            {**asdict(f), "severity": f.severity.value} for f in self.findings
        ]
        data["ok"] = self.ok
        data["is_html"] = self.is_html
        # The raw link lists are an internal artifact — on a 500-page crawl they
        # would dominate the report. The counts and graph metrics are the useful
        # part; `include_links` brings the lists back for debugging.
        data["outlink_count"] = len(self.outlinks)
        data["external_link_count"] = len(self.external_links)
        if not include_links:
            data.pop("outlinks", None)
            data.pop("external_links", None)
        # 128 integers per page is pure noise in a report.
        data.pop("sketch", None)
        if not include_html:
            data.pop("html", None)
        return data

    def to_json(self, *, include_html: bool = False, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(include_html=include_html), indent=indent,
                          ensure_ascii=False)
