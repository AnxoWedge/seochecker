"""The crawl orchestrator.

Ties together robots.txt, sitemaps, the frontier and the fetcher, and applies
the per-page analyzers as pages arrive. Politeness lives in the fetcher; what
lives here is the decision of *what* to ask for, and when to stop.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlsplit

from .analyzers import PageContext, run_page_analyzers
from .config import CrawlConfig
from .fetch import Fetcher
from .frontier import Frontier, Task
from .fingerprint import Detection, Fingerprinter
from .html import Document
from .models import Finding, Page
from .robots import RobotsTxt, parse as parse_robots, product_token
from .sitemap import SitemapSet, load_sitemaps
from .thresholds import Thresholds
from .urls import Scope, normalize

# The stack is a property of the site, not of each page, so only a sample of
# pages is fingerprinted. Without this, regex over raw HTML dominates a crawl.
FINGERPRINT_SAMPLE = 5

# Share of the page budget seeded from the sitemap before link discovery starts.
# Seeding the whole sitemap up front starves link discovery: on a small
# --max-pages the crawler would only ever see sitemap URLs and would never learn
# the link graph. Seeding none of it means sitemap entries are never validated on
# a large site, because links fill the budget first. A quarter, plus a top-up
# pass once links run out, gets both.
SITEMAP_SEED_SHARE = 4


@dataclass
class CrawlResult:
    pages: list[Page] = field(default_factory=list)
    technologies: list[Detection] = field(default_factory=list)
    site_findings: list[Finding] = field(default_factory=list)
    robots: RobotsTxt = field(default_factory=RobotsTxt)
    sitemap: SitemapSet = field(default_factory=SitemapSet)
    frontier: dict[str, Any] = field(default_factory=dict)
    stats: dict[str, Any] = field(default_factory=dict)
    stopped_because: str = ""


class Crawler:
    def __init__(self, config: CrawlConfig, *, on_page=None) -> None:
        self.config = config
        self.thresholds = Thresholds()
        self.scope = Scope(
            config.url,
            include_subdomains=config.include_subdomains,
            include=config.include_patterns,
            exclude=config.exclude_patterns,
        )
        self.frontier = Frontier(
            scope=self.scope,
            max_depth=config.max_depth,
            max_pages=config.max_pages,
        )
        self.agent = product_token(config.user_agent)
        self.fingerprinter = Fingerprinter(
            Path(config.rules) if config.rules else None,
            min_confidence=config.min_confidence,
        )
        self.result = CrawlResult()
        self.on_page = on_page
        self._sitemap_urls: set[str] = set()
        self._fingerprinted = 0
        self._merged: dict[str, Detection] = {}
        self._started = 0.0

    # --- setup -------------------------------------------------------------

    async def _load_robots(self, fetcher: Fetcher) -> RobotsTxt:
        origin = f"{urlsplit(self.config.url).scheme}://{urlsplit(self.config.url).netloc}"
        url = urljoin(origin + "/", "robots.txt")
        page = await fetcher.fetch(url)

        if page.error is not None:
            robots = RobotsTxt(source_url=url, error=page.error.value)
        elif page.status == 404 or (page.status and 400 <= page.status < 500):
            robots = RobotsTxt(source_url=url, status=page.status)  # no rules: crawl freely
        elif page.ok and page.html:
            robots = parse_robots(page.html, source_url=page.final_url)
            robots.fetched = True
            robots.status = page.status
        else:
            robots = RobotsTxt(source_url=url, status=page.status,
                               error=f"HTTP {page.status}")
        return robots

    def _apply_crawl_delay(self, fetcher: Fetcher) -> None:
        declared = self.result.robots.crawl_delay(self.agent)
        if declared is None:
            return
        host = urlsplit(self.config.url).netloc
        # Never speed up because robots.txt permits it — only ever slow down.
        fetcher.host_delay[host] = max(self.config.delay, declared)

    async def _discover_sitemaps(self, fetcher: Fetcher) -> SitemapSet:
        seeds = list(self.result.robots.sitemaps)
        if not seeds:
            origin = f"{urlsplit(self.config.url).scheme}://{urlsplit(self.config.url).netloc}"
            seeds = [f"{origin}/sitemap.xml"]
        return await load_sitemaps(fetcher, seeds, max_urls=self.config.max_pages * 4)

    # --- per page ----------------------------------------------------------

    def _allowed_by_robots(self, url: str) -> bool:
        if not self.config.obey_robots:
            return True
        return self.result.robots.is_allowed(url, self.agent)

    async def _process(self, task: Task, fetcher: Fetcher,
                       queue: asyncio.Queue) -> None:
        page = await fetcher.fetch(task.url, depth=task.depth, referrer=task.referrer or None)
        page.from_sitemap = task.from_sitemap

        doc = (
            Document(page.html, page.final_url)
            if page.error is None and page.is_html and page.html
            else None
        )

        technologies: list[Detection] = []
        if doc is not None and self._fingerprinted < FINGERPRINT_SAMPLE:
            self._fingerprinted += 1
            technologies = self.fingerprinter.detect(page, doc)
            for detection in technologies:
                current = self._merged.get(detection.name)
                if current is None or detection.confidence > current.confidence:
                    self._merged[detection.name] = detection

        page.findings = run_page_analyzers(
            PageContext(page=page, doc=doc, config=self.config,
                        thresholds=self.thresholds, technologies=technologies)
        )
        self.result.pages.append(page)
        if self.on_page:
            self.on_page(page, self.frontier)

        if doc is None or self.frontier.full:
            return

        for link in doc.links:
            candidate = normalize(link.url)
            if self.scope.allows(candidate) and not self._allowed_by_robots(candidate):
                self.frontier.refuse(candidate, "blocked by robots.txt")
                continue
            if nxt := self.frontier.consider(link.url, task.depth + 1, page.final_url):
                queue.put_nowait(nxt)

    # --- run ---------------------------------------------------------------

    def _seed_sitemap(self, queue: asyncio.Queue, urls: list[str],
                      start: int, limit: int) -> int:
        """Enqueue sitemap URLs from `start` until `limit` of them are accepted.

        Returns how far through the list it got, so a later pass can resume.
        """
        accepted = 0
        index = start
        while index < len(urls) and accepted < limit and not self.frontier.full:
            url = urls[index]
            index += 1
            if not self._allowed_by_robots(url):
                self.frontier.refuse(url, "blocked by robots.txt")
                continue
            if task := self.frontier.consider(url, 0, from_sitemap=True):
                queue.put_nowait(task)
                accepted += 1
        return index

    def _out_of_time(self) -> bool:
        budget = self.config.max_time
        return bool(budget) and (time.perf_counter() - self._started) > budget

    async def run(self) -> CrawlResult:
        self._started = time.perf_counter()
        config = self.config

        async with Fetcher(config) as fetcher:
            if config.obey_robots:
                self.result.robots = await self._load_robots(fetcher)
                self._apply_crawl_delay(fetcher)

            if config.use_sitemap:
                self.result.sitemap = await self._discover_sitemaps(fetcher)
                self._sitemap_urls = {normalize(url) for url in self.result.sitemap.urls}

            queue: asyncio.Queue = asyncio.Queue()

            if not self._allowed_by_robots(config.url):
                self.result.stopped_because = "the target URL is disallowed by robots.txt"
                self.frontier.refuse(config.url, "blocked by robots.txt")
            elif seed := self.frontier.consider(config.url, 0):
                queue.put_nowait(seed)

            sitemap_urls = self.result.sitemap.urls
            quota = max(1, config.max_pages // SITEMAP_SEED_SHARE) if sitemap_urls else 0
            seeded = self._seed_sitemap(queue, sitemap_urls, 0, quota)

            async def worker() -> None:
                while True:
                    task = await queue.get()
                    try:
                        if self._out_of_time():
                            self.result.stopped_because = "time budget reached"
                            continue
                        await self._process(task, fetcher, queue)
                    except Exception as exc:  # noqa: BLE001 — one bad page, not one bad crawl
                        self.result.stats.setdefault("worker_errors", []).append(
                            f"{task.url}: {type(exc).__name__}: {exc}"
                        )
                    finally:
                        queue.task_done()

            workers = [asyncio.create_task(worker()) for _ in range(max(1, config.concurrency))]
            await queue.join()

            # Link discovery is done. If budget remains, validate the rest of the
            # sitemap — those are the entries most likely to be orphans.
            while (seeded < len(sitemap_urls) and not self.frontier.full
                   and not self._out_of_time()):
                before = seeded
                seeded = self._seed_sitemap(queue, sitemap_urls, seeded, config.max_pages)
                if seeded == before or queue.empty():
                    break
                await queue.join()

            for task in workers:
                task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

            self.result.stats = {
                "requests": fetcher.requests_made,
                "bytes_downloaded": fetcher.bytes_downloaded,
                "duration_ms": round((time.perf_counter() - self._started) * 1000, 1),
                **self.result.stats,
            }

        if self.frontier.full and not self.result.stopped_because:
            self.result.stopped_because = f"page limit ({config.max_pages}) reached"

        self.result.technologies = sorted(
            self._merged.values(), key=lambda d: (-d.confidence, d.category, d.name)
        )
        self.result.frontier = self.frontier.summary()
        self.result.frontier["sitemap_urls"] = sorted(self._sitemap_urls)
        return self.result


async def crawl(config: CrawlConfig, *, on_page=None) -> CrawlResult:
    return await Crawler(config, on_page=on_page).run()
