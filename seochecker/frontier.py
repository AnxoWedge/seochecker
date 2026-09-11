"""The crawl frontier: what to visit next, and what to refuse.

Every rejection is counted with a reason. A crawl that quietly visits 12 pages
when you expected 500 is a debugging nightmare; a crawl that tells you it
skipped 488 URLs as off-site is self-explanatory.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .urls import Scope, normalize


@dataclass(slots=True)
class Task:
    url: str
    depth: int
    referrer: str = ""
    from_sitemap: bool = False


@dataclass(slots=True)
class Frontier:
    scope: Scope
    max_depth: int = 5
    max_pages: int = 500
    drop_tracking: bool = True

    seen: set[str] = field(default_factory=set)
    accepted: int = 0
    skipped: Counter = field(default_factory=Counter)
    blocked_urls: list[str] = field(default_factory=list)

    def consider(self, url: str, depth: int, referrer: str = "",
                 from_sitemap: bool = False) -> Task | None:
        """Return a Task if this URL should be crawled, else None (with a reason logged)."""
        canonical = normalize(url, drop_tracking=self.drop_tracking)

        if canonical in self.seen:
            self.skipped["already seen"] += 1
            return None
        if depth > self.max_depth:
            self.skipped["beyond max depth"] += 1
            return None
        if reason := self.scope.reason_to_skip(canonical):
            self.skipped[reason] += 1
            return None
        if self.accepted >= self.max_pages:
            self.skipped["page limit reached"] += 1
            return None

        self.seen.add(canonical)
        self.accepted += 1
        return Task(url=canonical, depth=depth, referrer=referrer, from_sitemap=from_sitemap)

    def refuse(self, url: str, reason: str) -> None:
        """Record a URL rejected outside the scope rules — robots.txt, mainly."""
        canonical = normalize(url, drop_tracking=self.drop_tracking)
        if canonical not in self.seen:
            self.seen.add(canonical)
        self.skipped[reason] += 1
        if reason == "blocked by robots.txt" and len(self.blocked_urls) < 50:
            self.blocked_urls.append(canonical)

    @property
    def full(self) -> bool:
        return self.accepted >= self.max_pages

    def summary(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "unique_urls_seen": len(self.seen),
            "skipped": dict(self.skipped.most_common()),
        }
