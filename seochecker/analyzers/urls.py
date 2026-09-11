"""URL health — readable, shallow, parameter-free URLs are easier to rank and to share."""

from __future__ import annotations

import re
from typing import Iterator
from urllib.parse import parse_qsl, urlsplit

from ..models import Finding
from ..urls import path_depth
from .base import PageContext, analyzer, notice, warning

_SESSION_PARAM = re.compile(r"^(phpsessid|jsessionid|sid|sessionid|session_id|cfid|cftoken)$", re.I)


@analyzer
def url_health(ctx: PageContext) -> Iterator[Finding]:
    url = ctx.url
    parts = urlsplit(url)
    limits = ctx.thresholds

    if len(url) > limits.url_max_length:
        yield notice(
            "url.too_long",
            f"URL is {len(url)} characters",
            evidence=url,
            fix=f"Long URLs get truncated in results and are awkward to share. Under "
                f"{limits.url_max_length} characters is a good target.",
        )

    depth = path_depth(url)
    if depth > limits.url_max_depth:
        yield notice(
            "url.too_deep",
            f"URL is {depth} levels deep",
            evidence=parts.path,
            fix="Deep paths suggest content buried far from the homepage. Flatter structures "
                "are crawled more thoroughly.",
        )

    if any(char.isupper() for char in parts.path):
        yield notice(
            "url.uppercase",
            "URL path contains uppercase characters",
            evidence=parts.path,
            fix="Most servers treat paths as case-sensitive, so /About and /about can become "
                "two indexed copies of one page. Lowercase throughout avoids it.",
        )

    if "_" in parts.path:
        yield notice(
            "url.underscores",
            "URL path uses underscores",
            evidence=parts.path,
            fix="Google treats hyphens as word separators and underscores as joiners, so "
                "my_page reads as one word. Use hyphens.",
        )

    if parts.query:
        params = parse_qsl(parts.query, keep_blank_values=True)
        if sessions := [name for name, _ in params if _SESSION_PARAM.match(name)]:
            yield warning(
                "url.session_id",
                f"URL carries a session identifier ({', '.join(sessions)})",
                evidence=url,
                fix="Session IDs in URLs generate an unbounded number of duplicate pages. Use "
                    "cookies instead.",
            )
        if len(params) > limits.url_max_params:
            yield notice(
                "url.many_parameters",
                f"URL has {len(params)} query parameters",
                evidence=parts.query,
                fix="Each combination is a distinct URL to a crawler. Prefer clean paths, and "
                    "canonicalise the variants you cannot avoid.",
            )
