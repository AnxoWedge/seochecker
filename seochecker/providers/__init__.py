"""Providers for data a crawl cannot see: field vitals, domain authority, backlinks.

Everything here is optional. The tool is complete without any of it; a provider
becomes available when a key is supplied, and says what it needs when not.
"""

from .base import AuthorityProvider, Metric, ProviderResult, UnconfiguredProvider
from .opr import OpenPageRank
from .psi import PageSpeedInsights


class Moz(UnconfiguredProvider):
    """Domain Authority, Page Authority and Spam Score.

    Left unimplemented on purpose: the Links API needs a paid subscription, so
    there is nothing to test against and no way to verify an implementation is
    right. The adapter shape is the two methods above — add them here when there
    is a key to check them with.
    """

    name = "Moz Links API"
    requires = "a paid Moz subscription (moz.com/products/api)"
    free = False


def configured(psi_key: str = "", opr_key: str = "") -> list[AuthorityProvider]:
    return [PageSpeedInsights(psi_key), OpenPageRank(opr_key), Moz()]


__all__ = ["AuthorityProvider", "Metric", "ProviderResult", "UnconfiguredProvider",
           "OpenPageRank", "PageSpeedInsights", "Moz", "configured"]
