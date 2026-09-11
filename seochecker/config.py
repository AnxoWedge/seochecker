"""Run configuration and the CLI that builds it.

One `CrawlConfig` drives the whole run. Fields that later phases will use are
declared now with safe defaults so the shape of the config stops changing.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from . import __version__

# A bot that says what it is. The "Mozilla/5.0 (compatible; ...)" shell is the
# convention every well-behaved crawler uses (Googlebot, Bingbot) — servers and
# log parsers expect it — and the token after it names us honestly.
BOT_UA = (
    f"Mozilla/5.0 (compatible; seochecker/{__version__}; +https://github.com/AnxoWedge/seochecker)"
)

# For auditing sites you control that reject unknown bots outright.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

UA_PRESETS = {"bot": BOT_UA, "browser": BROWSER_UA}

DEFAULT_ACCEPT = (
    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
    "image/webp,*/*;q=0.8"
)


@dataclass(slots=True)
class CrawlConfig:
    # --- target -------------------------------------------------------------
    url: str
    single: bool = False

    # --- crawl scope (Phase 4) ---------------------------------------------
    max_pages: int = 500
    max_depth: int = 5
    include_subdomains: bool = False
    include_patterns: list[re.Pattern[str]] = field(default_factory=list)
    exclude_patterns: list[re.Pattern[str]] = field(default_factory=list)
    obey_robots: bool = True
    use_sitemap: bool = True
    probe_soft_404: bool = True
    check_external: bool = False
    max_time: float = 0.0   # seconds; 0 = no limit

    # --- politeness ---------------------------------------------------------
    concurrency: int = 5
    delay: float = 0.25          # minimum seconds between requests to one host
    jitter: float = 0.25         # random extra delay, up to this many seconds
    timeout: float = 20.0
    max_retries: int = 2
    max_redirects: int = 10
    max_bytes: int = 5_000_000   # stop reading a body past this

    # --- transport ----------------------------------------------------------
    user_agent: str = BOT_UA
    accept_language: str = "en-US,en;q=0.9"
    extra_headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    http2: bool = True
    verify_tls: bool = True
    proxy: str | None = None

    # --- fingerprinting -----------------------------------------------------
    rules: str | None = None          # custom rules.yaml
    min_confidence: float = 0.5

    # --- output -------------------------------------------------------------
    out: str | None = None
    include_html: bool = False
    include_links: bool = False
    min_severity: str = "info"
    fail_on: str = "never"
    quiet: bool = False
    verbose: bool = False

    @property
    def host(self) -> str:
        return urlsplit(self.url).netloc


def normalize_target(raw: str) -> str:
    """Accept `example.com`, `example.com/path` or a full URL; return a full URL."""
    raw = raw.strip()
    if not raw:
        raise ValueError("empty URL")
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme: {parts.scheme!r}")
    if not parts.netloc:
        raise ValueError(f"could not parse a host out of {raw!r}")
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))


def _header_pair(value: str) -> tuple[str, str]:
    name, sep, val = value.partition(":")
    if not sep:
        raise argparse.ArgumentTypeError(f"header must be 'Name: value', got {value!r}")
    return name.strip(), val.strip()


def _cookie_pair(value: str) -> tuple[str, str]:
    name, sep, val = value.partition("=")
    if not sep:
        raise argparse.ArgumentTypeError(f"cookie must be 'name=value', got {value!r}")
    return name.strip(), val.strip()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="seocheck",
        description="Crawl a site and audit it for on-page SEO and technology stack.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("url", help="site or page to audit (scheme optional)")
    p.add_argument("--single", action="store_true",
                   help="fetch only this URL, do not crawl")
    p.add_argument("--version", action="version", version=f"seochecker {__version__}")

    scope = p.add_argument_group("scope")
    scope.add_argument("--max-pages", type=int, default=500)
    scope.add_argument("--max-depth", type=int, default=5)
    scope.add_argument("--subdomains", dest="include_subdomains", action="store_true",
                       help="follow links into subdomains of the target")
    scope.add_argument("--include", dest="include_patterns", action="append", default=[],
                       metavar="REGEX", help="only crawl URLs matching (repeatable)")
    scope.add_argument("--exclude", dest="exclude_patterns", action="append", default=[],
                       metavar="REGEX", help="skip URLs matching (repeatable)")
    scope.add_argument("--ignore-robots", dest="obey_robots", action="store_false",
                       help="ignore robots.txt — only for sites you own")
    scope.add_argument("--no-sitemap", dest="use_sitemap", action="store_false",
                       help="do not seed the crawl from sitemaps")
    scope.add_argument("--no-soft-404-probe", dest="probe_soft_404", action="store_false",
                       help="skip the one request that tests how the site handles a "
                            "URL that does not exist")
    scope.add_argument("--check-external", action="store_true",
                       help="also check that external links resolve (one HEAD request "
                            "per external URL, to third-party servers)")
    scope.add_argument("--max-time", type=float, default=0.0, metavar="SECONDS",
                       help="stop crawling after this long (0 = no limit)")

    pol = p.add_argument_group("politeness")
    pol.add_argument("-c", "--concurrency", type=int, default=5)
    pol.add_argument("--delay", type=float, default=0.25,
                     metavar="SECONDS", help="minimum gap between requests to one host")
    pol.add_argument("--jitter", type=float, default=0.25, metavar="SECONDS")
    pol.add_argument("--timeout", type=float, default=20.0, metavar="SECONDS")
    pol.add_argument("--retries", dest="max_retries", type=int, default=2)
    pol.add_argument("--max-redirects", type=int, default=10)
    pol.add_argument("--max-bytes", type=int, default=5_000_000)

    net = p.add_argument_group("transport")
    net.add_argument("--user-agent", default=None,
                     help=f"UA string, or a preset: {', '.join(UA_PRESETS)}")
    net.add_argument("--accept-language", default="en-US,en;q=0.9")
    net.add_argument("-H", "--header", dest="extra_headers", action="append",
                     default=[], type=_header_pair, metavar="'Name: value'")
    net.add_argument("--cookie", dest="cookies", action="append", default=[],
                     type=_cookie_pair, metavar="name=value")
    net.add_argument("--no-http2", dest="http2", action="store_false")
    net.add_argument("--insecure", dest="verify_tls", action="store_false",
                     help="do not verify TLS certificates")
    net.add_argument("--proxy", default=None, metavar="URL")

    fp = p.add_argument_group("fingerprinting")
    fp.add_argument("--rules", default=None, metavar="PATH",
                    help="custom technology rules file (defaults to the bundled rules.yaml)")
    fp.add_argument("--min-confidence", type=float, default=0.5, metavar="0..1",
                    help="hide technology detections below this confidence")

    out = p.add_argument_group("output")
    out.add_argument("-o", "--out", default=None, metavar="PATH",
                     help="write the JSON report here instead of stdout")
    out.add_argument("--include-html", action="store_true",
                     help="keep raw HTML in the JSON output (large)")
    out.add_argument("--include-links", action="store_true",
                     help="keep the per-page link lists in the JSON output (large)")
    out.add_argument("--min-severity", choices=["critical", "warning", "notice", "info"],
                     default="info", help="hide findings below this severity in the terminal")
    out.add_argument("--fail-on", choices=["critical", "warning", "notice", "never"],
                     default="never", help="exit non-zero if a finding at this severity or "
                                           "above is present")
    out.add_argument("-q", "--quiet", action="store_true")
    out.add_argument("-v", "--verbose", action="store_true")
    return p


def config_from_args(argv: list[str] | None = None) -> CrawlConfig:
    args = build_parser().parse_args(argv)
    ua = args.user_agent
    ua = UA_PRESETS.get(ua, ua) if ua else BOT_UA
    return CrawlConfig(
        url=normalize_target(args.url),
        single=args.single,
        max_pages=args.max_pages,
        max_depth=args.max_depth,
        include_subdomains=args.include_subdomains,
        include_patterns=[re.compile(r) for r in args.include_patterns],
        exclude_patterns=[re.compile(r) for r in args.exclude_patterns],
        obey_robots=args.obey_robots,
        use_sitemap=args.use_sitemap,
        probe_soft_404=args.probe_soft_404,
        check_external=args.check_external,
        max_time=args.max_time,
        concurrency=args.concurrency,
        delay=args.delay,
        jitter=args.jitter,
        timeout=args.timeout,
        max_retries=args.max_retries,
        max_redirects=args.max_redirects,
        max_bytes=args.max_bytes,
        user_agent=ua,
        accept_language=args.accept_language,
        extra_headers=dict(args.extra_headers),
        cookies=dict(args.cookies),
        http2=args.http2,
        verify_tls=args.verify_tls,
        proxy=args.proxy,
        rules=args.rules,
        min_confidence=args.min_confidence,
        out=args.out,
        include_html=args.include_html,
        include_links=args.include_links,
        min_severity=args.min_severity,
        fail_on=args.fail_on,
        quiet=args.quiet,
        verbose=args.verbose,
    )
