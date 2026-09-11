"""The polite async fetcher.

Responsibilities, in order of how much trouble they save later:

1. Never lose information the audit needs — every redirect hop, every header,
   timings, and the wire size are recorded, not just the final body.
2. Never hammer a host — requests to the same host are spaced by `delay` +
   jitter, and `Retry-After` is honoured on 429/503.
3. Never crash the run — network failures become a `Page` with an `error`, so a
   500-page crawl survives one bad certificate.
"""

from __future__ import annotations

import asyncio
import codecs
import random
import re
import ssl
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

import httpx

from .config import CrawlConfig, DEFAULT_ACCEPT
from .models import ErrorKind, Page, RedirectHop, Timing

# Statuses worth trying again. 429/503 are the polite-backoff pair; the rest are
# transient server-side hiccups.
RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

# Bodies we actually parse. Anything else we size up and drop on the floor.
TEXTUAL_MIMES = frozenset({
    "text/html", "application/xhtml+xml", "text/plain", "text/xml",
    "application/xml", "application/json", "application/ld+json",
    "application/rss+xml", "application/atom+xml",
})

_META_CHARSET = re.compile(
    r"""<meta[^>]+charset\s*=\s*["']?\s*([a-z0-9_\-:.]+)""", re.I
)
_META_HTTP_EQUIV = re.compile(
    r"""<meta[^>]+http-equiv\s*=\s*["']?content-type["']?[^>]*content\s*=\s*["'][^"']*charset\s*=\s*([a-z0-9_\-:.]+)""",
    re.I,
)
_BOMS = (
    (codecs.BOM_UTF8, "utf-8", 3),
    (codecs.BOM_UTF32_LE, "utf-32-le", 4),
    (codecs.BOM_UTF32_BE, "utf-32-be", 4),
    (codecs.BOM_UTF16_LE, "utf-16-le", 2),
    (codecs.BOM_UTF16_BE, "utf-16-be", 2),
)

# Headers that mean "you got a challenge, not the page" whatever the status is.
# AWS WAF famously answers with 202 Accepted, which otherwise looks like success.
_BLOCK_HEADERS = {
    "cf-mitigated": "Cloudflare challenge",
    "x-amzn-waf-action": "AWS WAF challenge",
    "x-datadome": "DataDome challenge",
    "x-sucuri-block": "Sucuri block",
}

# Body signatures. Lower precision than the headers, so these are only trusted on
# statuses where a challenge is plausible.
_BLOCK_SIGNATURES = (
    "just a moment...",
    "checking your browser before accessing",
    "attention required! | cloudflare",
    "verify you are human",
    "request unsuccessful. incapsula incident id",
    "awswafcookiedomainlist",
    "access denied</h1>",
    # Fastly / Imperva serve this one as a plain 200 with a real-looking document.
    "<title>client challenge",
    "/_fs-ch-",
    "please enable js and disable any ad blocker",
)
_CHALLENGE_STATUSES = frozenset({200, 202, 401, 403, 405, 429, 503})


class FetchFailure(Exception):
    def __init__(self, kind: ErrorKind, detail: str = "") -> None:
        super().__init__(detail or kind.value)
        self.kind = kind
        self.detail = detail


def parse_set_cookies(values: list[str]) -> dict[str, str]:
    """Cookie name -> value from raw Set-Cookie lines. Names are a fingerprint signal."""
    cookies: dict[str, str] = {}
    for line in values:
        pair = line.split(";", 1)[0]
        name, sep, value = pair.partition("=")
        if sep and (name := name.strip()):
            cookies[name] = value.strip()
    return cookies


@dataclass(slots=True)
class _Raw:
    """One completed HTTP exchange, before redirect logic gets involved."""

    url: str
    status: int
    reason: str
    headers: dict[str, str]
    cookies: dict[str, str]
    http_version: str
    body: bytes
    wire_bytes: int
    truncated: bool
    ttfb_ms: float
    download_ms: float
    attempts: int


def split_content_type(value: str) -> tuple[str, str]:
    """`text/html; charset=UTF-8` -> `('text/html', 'utf-8')`."""
    mime, _, params = value.partition(";")
    charset = ""
    for param in params.split(";"):
        key, _, val = param.partition("=")
        if key.strip().lower() == "charset":
            charset = val.strip().strip('"\'').lower()
    return mime.strip().lower(), charset


def _valid_charset(name: str) -> str:
    try:
        return codecs.lookup(name).name
    except (LookupError, TypeError, ValueError):
        return ""


def sniff_meta_charset(prefix: bytes) -> str:
    """Look for a declared charset in the first chunk of an HTML document."""
    head = prefix[:4096].decode("ascii", "ignore")
    for pattern in (_META_CHARSET, _META_HTTP_EQUIV):
        match = pattern.search(head)
        if match and (name := _valid_charset(match.group(1))):
            return name
    return ""


def decode_body(body: bytes, header_charset: str = "") -> tuple[str, str, str]:
    """Decode bytes to text. Returns (text, charset, where the charset came from).

    Priority follows the HTML spec's rough order: BOM, then the transport
    header, then an in-document declaration, then UTF-8, then a cp1252 rescue so
    a mis-declared legacy page still yields readable text.
    """
    for bom, encoding, length in _BOMS:
        if body.startswith(bom):
            return body[length:].decode(encoding, "replace"), encoding, "bom"

    if (name := _valid_charset(header_charset)):
        try:
            return body.decode(name), name, "header"
        except UnicodeDecodeError:
            pass

    if (name := sniff_meta_charset(body)):
        try:
            return body.decode(name), name, "meta"
        except UnicodeDecodeError:
            pass

    try:
        return body.decode("utf-8"), "utf-8", "default"
    except UnicodeDecodeError:
        return body.decode("cp1252", "replace"), "cp1252", "fallback"


def parse_retry_after(value: str) -> float | None:
    """`Retry-After` is either delta-seconds or an HTTP date. Accept both."""
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def looks_blocked(status: int, headers: dict[str, str], body: bytes) -> str:
    """Return a short reason if this response is bot mitigation, else ''."""
    for header, label in _BLOCK_HEADERS.items():
        if value := headers.get(header):
            return f"{label} ({header}: {value})"

    if status not in _CHALLENGE_STATUSES:
        return ""
    snippet = body[:4096].decode("utf-8", "ignore").lower()
    for signature in _BLOCK_SIGNATURES:
        if signature in snippet:
            server = headers.get("server", "")
            return f"bot mitigation{f' ({server})' if server else ''}: {signature.strip('|<> ')}"
    return ""


def classify_exception(exc: Exception) -> tuple[ErrorKind, str]:
    """Map an httpx exception onto our error taxonomy, and say if it's worth a retry."""
    cause: BaseException | None = exc
    while cause is not None:
        if isinstance(cause, ssl.SSLError):
            return ErrorKind.TLS, f"{type(cause).__name__}: {cause}"
        cause = cause.__context__ if cause.__context__ is not cause else None

    if isinstance(exc, httpx.TimeoutException):
        return ErrorKind.TIMEOUT, f"{type(exc).__name__}: {exc}"
    if isinstance(exc, (httpx.UnsupportedProtocol, httpx.InvalidURL)):
        return ErrorKind.INVALID_URL, str(exc)
    if isinstance(exc, httpx.TransportError):
        return ErrorKind.CONNECTION, f"{type(exc).__name__}: {exc}"
    return ErrorKind.UNKNOWN, f"{type(exc).__name__}: {exc}"


RETRYABLE_KINDS = frozenset({ErrorKind.TIMEOUT, ErrorKind.CONNECTION, ErrorKind.UNKNOWN})


class Fetcher:
    """Owns the httpx client and the per-host pacing state for one run."""

    def __init__(self, config: CrawlConfig) -> None:
        self.config = config
        self._client: httpx.AsyncClient | None = None
        self._semaphore = asyncio.Semaphore(max(1, config.concurrency))
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_next: dict[str, float] = {}
        self.host_delay: dict[str, float] = {}   # robots.txt Crawl-delay lands here
        self.requests_made = 0
        self.bytes_downloaded = 0

    # --- lifecycle ---------------------------------------------------------

    async def __aenter__(self) -> "Fetcher":
        cfg = self.config
        headers = {
            "User-Agent": cfg.user_agent,
            "Accept": DEFAULT_ACCEPT,
            "Accept-Language": cfg.accept_language,
            # httpx negotiates the encodings it can actually decode; brotli and
            # zstandard are installed so this is an honest advertisement.
            "Upgrade-Insecure-Requests": "1",
        }
        headers.update(cfg.extra_headers)
        self._client = httpx.AsyncClient(
            http2=cfg.http2,
            verify=cfg.verify_tls,
            follow_redirects=False,
            headers=headers,
            cookies=cfg.cookies or None,
            proxy=cfg.proxy,
            timeout=httpx.Timeout(cfg.timeout, connect=min(cfg.timeout, 10.0)),
            limits=httpx.Limits(
                max_connections=max(4, cfg.concurrency * 2),
                max_keepalive_connections=max(2, cfg.concurrency),
            ),
        )
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # --- pacing ------------------------------------------------------------

    async def _pace(self, host: str) -> None:
        """Space request *launches* to one host. Held briefly, so requests still overlap."""
        cfg = self.config
        gap = self.host_delay.get(host, cfg.delay)
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            now = time.monotonic()
            earliest = self._host_next.get(host, 0.0)
            if earliest > now:
                await asyncio.sleep(earliest - now)
            self._host_next[host] = time.monotonic() + gap + random.uniform(0, cfg.jitter)

    # --- one exchange ------------------------------------------------------

    async def _send_once(self, url: str, method: str) -> _Raw:
        assert self._client is not None, "use Fetcher as an async context manager"
        cfg = self.config
        started = time.perf_counter()
        request = self._client.build_request(method, url)
        response = await self._client.send(request, stream=True)
        ttfb_ms = (time.perf_counter() - started) * 1000
        try:
            headers = {k.lower(): v for k, v in response.headers.items()}
            cookies = parse_set_cookies(response.headers.get_list("set-cookie"))
            mime, _ = split_content_type(headers.get("content-type", ""))
            body = b""
            truncated = False
            if method != "HEAD" and (mime in TEXTUAL_MIMES or not mime):
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > cfg.max_bytes:
                        truncated = True
                        break
                body = b"".join(chunks)[: cfg.max_bytes]
            wire = response.num_bytes_downloaded or len(body)
            if not wire and (declared := headers.get("content-length", "")).isdigit():
                wire = int(declared)
            download_ms = (time.perf_counter() - started) * 1000 - ttfb_ms
        finally:
            await response.aclose()

        self.requests_made += 1
        self.bytes_downloaded += wire
        return _Raw(
            url=str(response.url),
            status=response.status_code,
            reason=response.reason_phrase,
            headers=headers,
            cookies=cookies,
            http_version=response.http_version,
            body=body,
            wire_bytes=wire,
            truncated=truncated,
            ttfb_ms=ttfb_ms,
            download_ms=max(0.0, download_ms),
            attempts=1,
        )

    async def _send_with_retries(self, url: str, method: str) -> _Raw:
        cfg = self.config
        host = urlsplit(url).netloc
        last_failure: FetchFailure | None = None

        for attempt in range(1, cfg.max_retries + 2):
            await self._pace(host)
            async with self._semaphore:
                try:
                    raw = await self._send_once(url, method)
                except Exception as exc:  # noqa: BLE001 — mapped, never swallowed
                    kind, detail = classify_exception(exc)
                    last_failure = FetchFailure(kind, detail)
                    if kind not in RETRYABLE_KINDS or attempt > cfg.max_retries:
                        raise last_failure from exc
                    await asyncio.sleep(self._backoff(attempt))
                    continue

            raw.attempts = attempt
            if raw.status in RETRY_STATUSES and attempt <= cfg.max_retries:
                await asyncio.sleep(
                    parse_retry_after(raw.headers.get("retry-after", ""))
                    or self._backoff(attempt)
                )
                continue
            return raw

        raise last_failure or FetchFailure(ErrorKind.UNKNOWN, "retries exhausted")

    def _backoff(self, attempt: int) -> float:
        """Exponential with full jitter, capped, so a struggling host gets room."""
        return min(30.0, random.uniform(0.5, 0.5 * 2**attempt))

    # --- public API --------------------------------------------------------

    async def fetch(
        self,
        url: str,
        *,
        depth: int = 0,
        referrer: str | None = None,
        method: str = "GET",
    ) -> Page:
        """Fetch one URL, following redirects by hand so every hop is recorded."""
        cfg = self.config
        page = Page(requested_url=url, depth=depth, referrer=referrer)
        started = time.perf_counter()
        current = url
        seen = {url}

        try:
            for _ in range(cfg.max_redirects + 1):
                raw = await self._send_with_retries(current, method)
                page.requests += raw.attempts
                page.retries += raw.attempts - 1

                location = raw.headers.get("location", "")
                if 300 <= raw.status < 400 and location:
                    target = urljoin(current, location.strip())
                    page.redirects.append(
                        RedirectHop(
                            url=current,
                            status=raw.status,
                            location=target,
                            elapsed_ms=round(raw.ttfb_ms + raw.download_ms, 1),
                        )
                    )
                    if target in seen:
                        page.error = ErrorKind.REDIRECT_LOOP
                        page.error_detail = f"{current} redirects back to {target}"
                        page.final_url = target
                        return self._finish(page, started)
                    seen.add(target)
                    current = target
                    continue

                return self._finish(self._apply(page, raw), started)

            page.error = ErrorKind.TOO_MANY_REDIRECTS
            page.error_detail = f"more than {cfg.max_redirects} redirects"
            page.final_url = current
            return self._finish(page, started)

        except FetchFailure as failure:
            page.error = failure.kind
            page.error_detail = failure.detail
            page.final_url = current
            return self._finish(page, started)

    def _apply(self, page: Page, raw: _Raw) -> Page:
        """Copy a terminal response onto the Page."""
        mime, header_charset = split_content_type(raw.headers.get("content-type", ""))
        page.final_url = raw.url
        page.status = raw.status
        page.reason = raw.reason
        page.http_version = raw.http_version
        page.headers = raw.headers
        page.cookies = raw.cookies
        page.content_type = raw.headers.get("content-type", "")
        page.mime = mime
        page.wire_bytes = raw.wire_bytes
        page.truncated = raw.truncated
        page.timing.ttfb_ms = round(raw.ttfb_ms, 1)
        page.timing.download_ms = round(raw.download_ms, 1)

        if raw.body and (mime in TEXTUAL_MIMES or not mime):
            text, charset, source = decode_body(raw.body, header_charset)
            page.html = text
            page.charset = charset
            page.charset_source = source
            page.decoded_bytes = len(raw.body)
        elif header_charset:
            page.charset = header_charset
            page.charset_source = "header"

        if reason := looks_blocked(raw.status, raw.headers, raw.body):
            page.error = ErrorKind.BLOCKED
            page.error_detail = reason
        return page

    @staticmethod
    def _finish(page: Page, started: float) -> Page:
        page.timing.total_ms = round((time.perf_counter() - started) * 1000, 1)
        return page
