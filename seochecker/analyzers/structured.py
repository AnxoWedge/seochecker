"""JSON-LD structured data: is it there, is it valid, is it complete enough."""

from __future__ import annotations

from typing import Any, Iterator

from ..models import Finding
from .base import PageContext, analyzer, info, notice, sample, warning

# The properties Google's rich-result documentation treats as required. Kept
# deliberately short — this is a completeness hint, not a validator.
REQUIRED_PROPERTIES: dict[str, tuple[str, ...]] = {
    "Article": ("headline", "datePublished"),
    "NewsArticle": ("headline", "datePublished"),
    "BlogPosting": ("headline", "datePublished"),
    "Product": ("name", "image"),
    "Offer": ("price", "priceCurrency"),
    "LocalBusiness": ("name", "address"),
    "Organization": ("name",),
    "BreadcrumbList": ("itemListElement",),
    "FAQPage": ("mainEntity",),
    "Recipe": ("name", "image", "recipeIngredient"),
    "Event": ("name", "startDate", "location"),
    "VideoObject": ("name", "thumbnailUrl", "uploadDate"),
    "JobPosting": ("title", "datePosted", "hiringOrganization"),
}


def _typed_nodes(blocks: Any) -> Iterator[dict[str, Any]]:
    """Every dict carrying an @type, wherever it sits in the graph."""
    if isinstance(blocks, dict):
        if "@type" in blocks:
            yield blocks
        for value in blocks.values():
            yield from _typed_nodes(value)
    elif isinstance(blocks, list):
        for item in blocks:
            yield from _typed_nodes(item)


def _types_of(node: dict[str, Any]) -> list[str]:
    value = node.get("@type")
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


@analyzer
def structured_data(ctx: PageContext) -> Iterator[Finding]:
    if not ctx.doc:
        return
    blocks = ctx.doc.json_ld

    broken = [b for b in blocks if isinstance(b, dict) and "__parse_error__" in b]
    if broken:
        yield warning(
            "structured.invalid_json",
            f"{len(broken)} JSON-LD block(s) contain invalid JSON — search engines discard these",
            evidence=sample([b["__parse_error__"] for b in broken], 2),
            fix="Fix the syntax. A trailing comma or an unescaped quote is enough to void the "
                "whole block.",
        )

    valid = [b for b in blocks if not (isinstance(b, dict) and "__parse_error__" in b)]
    microdata = ctx.doc.tree.css_first("[itemscope]") is not None

    if not valid:
        if microdata:
            yield info(
                "structured.microdata_only",
                "Structured data uses microdata, not JSON-LD",
                fix="Microdata is still read, but Google recommends JSON-LD — it is easier to "
                    "maintain and does not entangle markup with data.",
            )
        else:
            yield notice(
                "structured.none",
                "No structured data on the page",
                fix="Add JSON-LD for what this page is — Organization and WebSite sitewide, "
                    "plus Product, Article, LocalBusiness or BreadcrumbList as appropriate. "
                    "It is what makes rich results possible.",
            )
        return

    nodes = list(_typed_nodes(valid))
    found_types = sorted({t for node in nodes for t in _types_of(node)})
    yield info(
        "structured.present",
        f"Structured data: {', '.join(found_types)}" if found_types
        else "Structured data present but no @type declared",
        evidence=f"{len(valid)} JSON-LD block(s), {len(nodes)} typed node(s)",
    )

    incomplete: list[str] = []
    for node in nodes:
        for type_name in _types_of(node):
            required = REQUIRED_PROPERTIES.get(type_name)
            if not required:
                continue
            if missing := [prop for prop in required if not node.get(prop)]:
                incomplete.append(f"{type_name} missing {', '.join(missing)}")
    if incomplete:
        yield notice(
            "structured.missing_properties",
            f"{len(incomplete)} structured-data node(s) missing required properties",
            evidence=sample(sorted(set(incomplete)), 4),
            fix="Rich results are only granted when the required properties are present. "
                "Check the specific type in Google's rich results documentation.",
        )
