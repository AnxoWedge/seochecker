"""Open PageRank: a free, domain-level authority score.

Not Google's PageRank, and not Moz's Domain Authority — an independent 0-10
estimate built from an open link graph. Useful as a rough sense of a domain's
standing, and free with a signup key, which is why it is here and Moz is not.
"""

from __future__ import annotations

from .base import Metric, ProviderResult, UnconfiguredProvider

ENDPOINT = "https://openpagerank.com/api/v1.0/getPageRank"


class OpenPageRank(UnconfiguredProvider):
    name = "Open PageRank"
    requires = "a free API key (domcop.com/openpagerank)"
    free = True

    def __init__(self, api_key: str = "", *, timeout: float = 30.0) -> None:
        self.api_key = api_key
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    async def domain_metrics(self, host: str) -> ProviderResult:
        if not self.available:
            return self._unavailable(host)
        import httpx

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    ENDPOINT, params={"domains[]": host},
                    headers={"API-OPR": self.api_key})
        except Exception as exc:  # noqa: BLE001
            return ProviderResult(provider=self.name, target=host,
                                  error=f"{type(exc).__name__}: {exc}")

        if response.status_code != 200:
            return ProviderResult(provider=self.name, target=host,
                                  error=f"HTTP {response.status_code}: {response.text[:160]}")

        entries = (response.json() or {}).get("response") or []
        if not entries or entries[0].get("status_code") != 200:
            return ProviderResult(provider=self.name, target=host,
                                  error="no rank recorded for this domain")

        entry = entries[0]
        return ProviderResult(provider=self.name, target=host, metrics=[
            Metric(name="Open PageRank", value=entry.get("page_rank_decimal"),
                   unit="/10", source="openpagerank.com",
                   note="an independent estimate, not Google's PageRank"),
            Metric(name="Rank position", value=entry.get("rank"),
                   source="openpagerank.com"),
        ])

    async def page_metrics(self, url: str) -> ProviderResult:
        from urllib.parse import urlsplit
        return ProviderResult(provider=self.name, target=url,
                              error="Open PageRank is domain-level only")
