"""The authority-provider interface.

Some things a crawl cannot see. Backlinks, domain authority and real-user Core
Web Vitals all live behind somebody's API, and every one of those APIs wants a
key. So the shape is: the tool works fully without any of them, and each provider
becomes available the moment a key is supplied.

Every provider states what it costs and what it needs, because "why is this
empty" should have an answer in the output rather than in the source.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass(slots=True)
class Metric:
    """One number from a provider, with enough context to be read correctly."""

    name: str
    value: float | str | None
    unit: str = ""
    source: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value, "unit": self.unit,
                "source": self.source, "note": self.note}


@dataclass(slots=True)
class ProviderResult:
    provider: str
    target: str
    metrics: list[Metric] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.metrics)

    def to_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "target": self.target,
                "metrics": [m.to_dict() for m in self.metrics],
                "error": self.error or None}


@runtime_checkable
class AuthorityProvider(Protocol):
    """What every provider has to offer, whether or not it is configured."""

    name: str
    requires: str        # what a user must obtain to enable it
    free: bool

    @property
    def available(self) -> bool:
        """True when this provider has what it needs to answer."""
        ...

    async def domain_metrics(self, host: str) -> ProviderResult: ...

    async def page_metrics(self, url: str) -> ProviderResult: ...


class UnconfiguredProvider:
    """Base behaviour for a provider with no credentials: say so, clearly."""

    name = "unnamed"
    requires = "an API key"
    free = False

    @property
    def available(self) -> bool:
        return False

    def _unavailable(self, target: str) -> ProviderResult:
        return ProviderResult(
            provider=self.name,
            target=target,
            error=f"{self.name} is not configured — it needs {self.requires}",
        )

    async def domain_metrics(self, host: str) -> ProviderResult:
        return self._unavailable(host)

    async def page_metrics(self, url: str) -> ProviderResult:
        return self._unavailable(url)
