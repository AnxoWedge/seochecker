"""Background crawl execution for the dashboard.

A crawl is async and long-running; Flask is synchronous and per-request. So each
run gets a thread with its own event loop, and the request handlers only ever
read a small state object. Nothing here blocks a request.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from ..cli import run_crawl, scorecard
from ..config import CrawlConfig
from ..crawl import CrawlResult
from ..models import Severity
from ..score import Scorecard

# Crawls are network-bound and polite, so a couple at once is plenty. More would
# mostly mean more memory held for results.
MAX_CONCURRENT = 2
# Finished results are kept so their reports stay viewable; beyond this, the
# oldest are dropped. A large crawl result is tens of megabytes.
KEEP_RESULTS = 6


@dataclass
class RunState:
    id: str
    target: str
    config: CrawlConfig
    status: str = "running"          # running | done | failed | cancelled
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    pages_done: int = 0
    pages_known: int = 1
    current_url: str = ""
    counts: dict[str, int] = field(default_factory=lambda: {
        "critical": 0, "warning": 0, "notice": 0, "info": 0})
    result: CrawlResult | None = None
    card: Scorecard | None = None
    error: str = ""
    stop_event: threading.Event = field(default_factory=threading.Event)

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def elapsed(self) -> float:
        return (self.finished_at or time.time()) - self.started_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "target": self.target,
            "status": self.status,
            "pages_done": self.pages_done,
            "pages_known": max(self.pages_known, self.pages_done),
            "current_url": self.current_url,
            "counts": dict(self.counts),
            "elapsed": round(self.elapsed, 1),
            "error": self.error,
            "score": round(self.card.overall, 1) if self.card else None,
            "grade": self.card.grade if self.card else None,
            "stopped_because": self.result.stopped_because if self.result else "",
        }


class RunManager:
    def __init__(self) -> None:
        self._runs: dict[str, RunState] = {}
        self._lock = threading.Lock()

    # --- queries -----------------------------------------------------------

    def get(self, run_id: str) -> RunState | None:
        with self._lock:
            return self._runs.get(run_id)

    def recent(self, limit: int = 20) -> list[RunState]:
        with self._lock:
            runs = sorted(self._runs.values(), key=lambda r: -r.started_at)
        return runs[:limit]

    @property
    def active(self) -> int:
        with self._lock:
            return sum(1 for r in self._runs.values() if r.running)

    # --- lifecycle ---------------------------------------------------------

    def start(self, config: CrawlConfig) -> RunState:
        if self.active >= MAX_CONCURRENT:
            raise RuntimeError(
                f"{MAX_CONCURRENT} crawls are already running — wait for one to finish"
            )

        state = RunState(id=secrets.token_hex(8), target=config.url, config=config)
        with self._lock:
            self._runs[state.id] = state
        threading.Thread(target=self._execute, args=(state,), daemon=True).start()
        return state

    def cancel(self, run_id: str) -> bool:
        state = self.get(run_id)
        if state is None or not state.running:
            return False
        state.stop_event.set()
        return True

    # --- the worker --------------------------------------------------------

    def _execute(self, state: RunState) -> None:
        def on_page(page, frontier) -> None:
            state.pages_done += 1
            state.pages_known = max(frontier.accepted, state.pages_done)
            state.current_url = page.final_url
            tally = Counter(f.severity for f in page.findings)
            for severity in Severity:
                state.counts[severity.value] += tally[severity]

        try:
            result = asyncio.run(run_crawl(
                state.config, on_page=on_page, should_stop=state.stop_event.is_set,
            ))
            state.result = result
            state.card = scorecard(state.config, result)
            # Site-level findings only exist once the crawl finishes.
            site_tally = Counter(f.severity for f in result.site_findings)
            for severity in Severity:
                state.counts[severity.value] += site_tally[severity]
            state.status = "cancelled" if state.stop_event.is_set() else "done"
        except Exception as exc:  # noqa: BLE001 — surfaced in the UI, never swallowed
            state.status = "failed"
            state.error = f"{type(exc).__name__}: {exc}"
        finally:
            state.finished_at = time.time()
            self._evict()

    def _evict(self) -> None:
        """Drop the results of old finished runs, keeping their summary rows."""
        with self._lock:
            finished = sorted(
                (r for r in self._runs.values() if not r.running and r.result is not None),
                key=lambda r: -(r.finished_at or 0),
            )
        for state in finished[KEEP_RESULTS:]:
            state.result = None


def host_of(url: str) -> str:
    return urlsplit(url).netloc or url
