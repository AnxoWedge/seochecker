"""On-page analyzers.

Importing this package registers every check. Order of import does not matter —
findings are sorted by severity before they are reported.
"""

from .base import (  # noqa: F401
    PageContext, REGISTRY, SITE_REGISTRY, SiteContext,
    run_page_analyzers, run_site_analyzers,
)

from . import (  # noqa: F401  (imported for the side effect of registering)
    content,
    duplicates,
    headings,
    i18n,
    images,
    links,
    meta,
    site,
    social,
    structure,
    structured,
    technical,
    urls,
)

__all__ = [
    "PageContext", "SiteContext", "REGISTRY", "SITE_REGISTRY",
    "run_page_analyzers", "run_site_analyzers",
]
