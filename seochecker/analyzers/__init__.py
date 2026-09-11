"""On-page analyzers.

Importing this package registers every check. Order of import does not matter —
findings are sorted by severity before they are reported.
"""

from .base import PageContext, run_page_analyzers, REGISTRY  # noqa: F401

from . import (  # noqa: F401  (imported for the side effect of registering)
    content,
    headings,
    i18n,
    images,
    links,
    meta,
    social,
    structured,
    technical,
)

__all__ = ["PageContext", "run_page_analyzers", "REGISTRY"]
