"""Language declaration and hreflang."""

from __future__ import annotations

import re
from typing import Iterator

from ..models import Finding
from .base import PageContext, analyzer, notice, sample, warning

# `html lang` may legitimately carry variants and extensions (`de-CH-1901`), so
# it only has to look like a language tag.
_LANG_LOOSE = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$", re.I)

# hreflang is narrower: language, optional script, optional region — and nothing
# else. Keeping this strict is the point, because the errors it catches are the
# common ones: `pt_PT` with an underscore, or a spelled-out `portugues`.
_LANG_STRICT = re.compile(r"^[a-z]{2,3}(-[a-z]{4})?(-([a-z]{2}|[0-9]{3}))?$", re.I)


def _valid_lang(value: str, *, allow_default: bool = False) -> bool:
    if allow_default:
        if value.lower() == "x-default":
            return True
        return bool(_LANG_STRICT.match(value))
    return bool(_LANG_LOOSE.match(value))


@analyzer
def language(ctx: PageContext) -> Iterator[Finding]:
    if not ctx.doc:
        return
    lang = ctx.doc.lang

    if not lang:
        yield warning(
            "i18n.missing_lang",
            "<html> has no lang attribute",
            fix="Add lang=\"pt-PT\" (or whichever applies). Screen readers use it to pick a "
                "voice, and it helps search engines serve the page to the right audience.",
        )
    elif not _valid_lang(lang):
        yield notice(
            "i18n.invalid_lang",
            f"lang attribute {lang!r} is not a valid language tag",
            fix="Use a BCP 47 tag: 'pt', 'pt-PT', 'en-GB'.",
        )


@analyzer
def hreflang(ctx: PageContext) -> Iterator[Finding]:
    if not ctx.doc:
        return
    alternates = ctx.doc.hreflangs
    if not alternates:
        return  # a single-language site is not required to have any

    if invalid := [lang for lang, _, _ in alternates
                   if not _valid_lang(lang, allow_default=True)]:
        yield warning(
            "i18n.hreflang_invalid",
            f"{len(invalid)} invalid hreflang value(s) — invalid entries are ignored entirely",
            evidence=sample(invalid),
            fix="Use a language, or language-region, code: 'pt', 'pt-PT', 'x-default'. "
                "Region alone ('PT') is not valid.",
        )

    targets = {resolved.rstrip("/") for _, _, resolved in alternates}
    if ctx.url.rstrip("/") not in targets:
        yield warning(
            "i18n.hreflang_no_self",
            "hreflang set does not include this page",
            evidence=sample(sorted(targets), 3),
            fix="Every page in an hreflang cluster must list itself as well as its "
                "alternates, or the whole cluster is disregarded.",
        )

    if not any(lang.lower() == "x-default" for lang, _, _ in alternates):
        yield notice(
            "i18n.hreflang_no_xdefault",
            "No x-default in the hreflang set",
            fix="Add an x-default pointing at the version to serve when no language matches.",
        )

    if relative := [raw for _, raw, _ in alternates
                    if not raw.lower().startswith(("http://", "https://"))]:
        yield notice(
            "i18n.hreflang_relative",
            f"{len(relative)} hreflang URL(s) are relative",
            evidence=sample(relative),
            fix="Use absolute URLs in hreflang annotations.",
        )
