"""PageSpeed Insights: real Core Web Vitals, from real Chrome users.

This is the field data Google actually ranks on — the 75th percentile across
real devices and networks — as opposed to the lab measurement in `vitals.py`.
Google is explicit that lab measurement is not a substitute for it.

It needs a key. The keyless endpoint shares one quota across everybody who calls
it anonymously, and in practice that quota is exhausted: a keyless request
returns 429. A key is free from the Google Cloud console.

Field data only exists for URLs with enough real traffic. For a quiet site the
API answers with nothing, which is not an error and is reported as such.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from .base import Metric, ProviderResult, UnconfiguredProvider

ENDPOINT = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

# The API's metric names, and what they are.
FIELD_METRICS = {
    "LARGEST_CONTENTFUL_PAINT_MS": ("Largest Contentful Paint", "ms"),
    "INTERACTION_TO_NEXT_PAINT": ("Interaction to Next Paint", "ms"),
    "CUMULATIVE_LAYOUT_SHIFT_SCORE": ("Cumulative Layout Shift", ""),
    "FIRST_CONTENTFUL_PAINT_MS": ("First Contentful Paint", "ms"),
    "EXPERIMENTAL_TIME_TO_FIRST_BYTE": ("Time to First Byte", "ms"),
}


class PageSpeedInsights(UnconfiguredProvider):
    name = "PageSpeed Insights"
    requires = "a free Google API key (console.cloud.google.com)"
    free = True

    def __init__(self, api_key: str = "", *, strategy: str = "mobile",
                 timeout: float = 60.0) -> None:
        self.api_key = api_key
        self.strategy = strategy
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    async def _query(self, target: str) -> dict[str, Any] | str:
        import httpx

        params = {"url": target, "strategy": self.strategy, "key": self.api_key}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(ENDPOINT, params=params)
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"

        if response.status_code != 200:
            try:
                message = response.json()["error"]["message"]
            except Exception:  # noqa: BLE001
                message = response.text[:200]
            return f"HTTP {response.status_code}: {message}"
        return response.json()

    @staticmethod
    def _extract(experience: dict[str, Any], scope: str) -> list[Metric]:
        metrics: list[Metric] = []
        for key, entry in (experience.get("metrics") or {}).items():
            label, unit = FIELD_METRICS.get(key, (key, ""))
            metrics.append(Metric(
                name=label,
                value=entry.get("percentile"),
                unit=unit,
                source=f"CrUX field data ({scope}, 75th percentile)",
                note=str(entry.get("category", "")).lower().replace("_", " "),
            ))
        return metrics

    async def page_metrics(self, url: str) -> ProviderResult:
        if not self.available:
            return self._unavailable(url)
        payload = await self._query(url)
        if isinstance(payload, str):
            return ProviderResult(provider=self.name, target=url, error=payload)

        metrics = self._extract(payload.get("loadingExperience") or {}, "this URL")
        if not metrics:
            metrics = self._extract(payload.get("originLoadingExperience") or {},
                                    "whole origin")
        lighthouse = (payload.get("lighthouseResult") or {}).get("categories") or {}
        for category in ("performance", "seo", "accessibility"):
            score = (lighthouse.get(category) or {}).get("score")
            if score is not None:
                metrics.append(Metric(name=f"Lighthouse {category}", value=round(score * 100),
                                      unit="/100", source="PageSpeed Insights lab run"))
        if not metrics:
            return ProviderResult(
                provider=self.name, target=url,
                error="no field data for this URL — it needs enough real traffic "
                      "to appear in the Chrome UX Report",
            )
        return ProviderResult(provider=self.name, target=url, metrics=metrics)

    async def domain_metrics(self, host: str) -> ProviderResult:
        return await self.page_metrics(f"https://{host}/")
