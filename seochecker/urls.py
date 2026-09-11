"""URL normalization and crawl scope.

Normalization decides what counts as the same page. Get it wrong in one
direction and the crawler fetches `/about`, `/about?utm_source=x` and
`/ABOUT` as three pages; get it wrong in the other and it silently skips
genuinely distinct URLs. Both failures are quiet, so the rules here are
deliberately conservative: strip only what is provably meaningless.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

DEFAULT_PORTS = {"http": "80", "https": "443"}

# Parameters that identify a marketing campaign, never a page. Dropping them
# collapses a lot of duplicate crawling; keeping anything ambiguous (`ref`,
# `page`, `id`) is the safer default.
TRACKING_PARAMS = frozenset({
    "gclid", "gclsrc", "dclid", "gbraid", "wbraid",
    "fbclid", "igshid", "igsh", "msclkid", "yclid", "ysclid",
    "ttclid", "twclid", "li_fat_id", "epik", "s_kwcid",
    "mc_cid", "mc_eid", "_ga", "_gl",
    "_hsenc", "_hsmi", "__hstc", "__hssc", "__hsfp", "hsCtaTracking",
    "vero_conv", "vero_id", "oly_anon_id", "oly_enc_id",
    "zanpid", "ranmid", "raneaid", "ransiteid",
    "ref_src", "ref_url",
})
TRACKING_PREFIXES = ("utm_", "pk_", "piwik_", "mtm_", "hsa_", "matomo_")

# Session identifiers carried in the path, e.g. `/page;jsessionid=ABC123`
_PATH_SESSION = re.compile(r";(jsessionid|phpsessid|sid|cfid|cftoken)=[^;/?]*", re.I)

# Characters RFC 3986 says never need escaping — decoding them is safe and makes
# `%7Euser` and `~user` compare equal.
_UNRESERVED = re.compile(r"%(2[DE]|3[0-9]|[46][1-9A-F]|[57][0-9A]|5F|7E)", re.I)


def is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PREFIXES)


def _normalize_percent_encoding(value: str) -> str:
    """Uppercase hex digits, then decode anything that never needed encoding."""
    value = re.sub(r"%([0-9a-fA-F]{2})", lambda m: "%" + m.group(1).upper(), value)
    return _UNRESERVED.sub(lambda m: unquote("%" + m.group(1)), value)


def normalize(url: str, *, drop_tracking: bool = True) -> str:
    """Canonical form used for deduplication.

    Lowercases scheme and host, drops the fragment and default port, removes
    tracking parameters, and sorts what is left. Path case and trailing slashes
    are preserved — servers are allowed to treat those as significant, and
    plenty do.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url

    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    port = "" if parts.port is None or str(parts.port) == DEFAULT_PORTS.get(scheme) \
        else f":{parts.port}"
    netloc = f"{host}{port}"

    path = _PATH_SESSION.sub("", parts.path) or "/"
    path = _normalize_percent_encoding(path)

    query = ""
    if parts.query:
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        if drop_tracking:
            pairs = [(k, v) for k, v in pairs if not is_tracking_param(k)]
        query = urlencode(sorted(pairs), quote_via=quote)

    return urlunsplit((scheme, netloc, path, query, ""))


def base_host(host: str) -> str:
    """`www.example.com` -> `example.com`, so the two are one site by default."""
    host = host.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def path_depth(url: str) -> int:
    """Number of path segments. `/` is 0, `/a/b/` is 2."""
    return len([segment for segment in urlsplit(url).path.split("/") if segment])


class Scope:
    """Decides whether a discovered URL belongs to this crawl.

    Default is the target's own site, treating `www.` and the bare host as one.
    `include_subdomains` widens that to anything under the same base host — an
    anchored suffix match, so `evil-example.com` does not sneak past
    `example.com`.
    """

    def __init__(self, target: str, *, include_subdomains: bool = False,
                 include: list[re.Pattern[str]] | None = None,
                 exclude: list[re.Pattern[str]] | None = None) -> None:
        parts = urlsplit(target)
        self.target = target
        self.host = (parts.hostname or "").lower()
        self.base = base_host(self.host)
        self.scheme = parts.scheme
        self.include_subdomains = include_subdomains
        self.include = include or []
        self.exclude = exclude or []

    def host_in_scope(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        if not host:
            return False
        if self.include_subdomains:
            return host == self.base or host.endswith("." + self.base)
        return base_host(host) == self.base

    def reason_to_skip(self, url: str) -> str:
        """Empty string means crawl it; otherwise a short reason, for the report."""
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            return "unsupported scheme"
        if not self.host_in_scope(url):
            return "off-site"
        if self.exclude and any(p.search(url) for p in self.exclude):
            return "excluded by pattern"
        if self.include and not any(p.search(url) for p in self.include):
            return "not matched by --include"
        return ""

    def allows(self, url: str) -> bool:
        return not self.reason_to_skip(url)
