"""Tests from an audit pass: internal coherence, and gaps against official guidance.

Each of these exists because the audit found the app contradicting itself or
contradicting what Google and Apple actually publish.
"""

from __future__ import annotations

import asyncio
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seochecker.analyzers import PageContext, SiteContext, run_page_analyzers, run_site_analyzers
from seochecker.config import CrawlConfig
from seochecker.html import Document
from seochecker.models import Page, Severity
from seochecker.robots import parse as parse_robots
from seochecker.thresholds import Thresholds

ANALYZER_DIR = Path(__file__).resolve().parent.parent / "seochecker" / "analyzers"


def analyse(html: str, *, url: str = "https://e.test/p", headers: dict | None = None) -> set[str]:
    page = Page(requested_url=url, final_url=url, status=200, mime="text/html",
                headers={"content-type": "text/html", **(headers or {})}, html=html)
    page.timing.ttfb_ms = 100
    return {f.id for f in run_page_analyzers(PageContext(
        page=page, doc=Document(html, url), config=CrawlConfig(url=url),
        thresholds=Thresholds()))}


def site(pages: list[Page], robots_text: str = "") -> set[str]:
    robots = parse_robots(robots_text, source_url="https://e.test/robots.txt")
    robots.fetched = bool(robots_text)
    robots.status = 200 if robots_text else None
    context = SiteContext(config=CrawlConfig(url="https://e.test/"), pages=pages, robots=robots)
    return {f.id: f for f in run_site_analyzers(context)}


class FindingIdTests(unittest.TestCase):
    def test_no_finding_id_is_defined_twice(self):
        """Two findings sharing an id collapse into one group in the report and one
        deduction in the score, even when they mean different things."""
        counts: dict[str, int] = {}
        for path in ANALYZER_DIR.glob("*.py"):
            for match in re.finditer(r'^\s+"([a-z_]+\.[a-z_0-9]+)",\s*$',
                                     path.read_text(), re.M):
                counts[match.group(1)] = counts.get(match.group(1), 0) + 1
        repeated = {fid: n for fid, n in counts.items() if n > 1}
        self.assertEqual(repeated, {}, f"finding ids defined more than once: {repeated}")


class NoindexTests(unittest.TestCase):
    """A page kept out of search should not be judged on how it would look in search."""

    BODY = ('<title>x</title></head><body><p>short</p><img src="a.png">'
            "<h2>heading</h2></body></html>")

    def indexable(self) -> set[str]:
        return analyse(f'<html lang="en"><head>{self.BODY}')

    def noindexed(self) -> set[str]:
        return analyse(f'<html lang="en"><head><meta name="robots" content="noindex">{self.BODY}')

    def test_appearance_findings_are_suppressed(self):
        gone = self.indexable() - self.noindexed()
        for suppressed in ("title.too_short", "description.missing", "canonical.missing",
                           "social.og_missing", "structured.none", "content.very_thin"):
            with self.subTest(finding=suppressed):
                self.assertIn(suppressed, gone)

    def test_everything_that_still_matters_is_kept(self):
        kept = self.noindexed()
        # Accessibility, correctness and crawlability do not stop mattering.
        for retained in ("images.missing_alt", "technical.no_viewport",
                         "technical.no_charset", "indexability.noindex"):
            with self.subTest(finding=retained):
                self.assertIn(retained, kept)

    def test_x_robots_tag_counts_too(self):
        found = analyse(f'<html lang="en"><head>{self.BODY}',
                        headers={"x-robots-tag": "noindex"})
        self.assertNotIn("title.too_short", found)
        self.assertIn("indexability.noindex", found)

    def test_an_ordinary_page_is_unaffected(self):
        self.assertIn("title.too_short", self.indexable())


class RobotsGuidanceTests(unittest.TestCase):
    """Checks added from what Google and Apple actually publish."""

    def page_with_resources(self, resources: list[str]) -> Page:
        page = Page(requested_url="https://e.test/", final_url="https://e.test/",
                    status=200, mime="text/html", headers={})
        page.seo = {"resources": resources}
        return page

    def test_blocking_a_search_engine_is_critical(self):
        found = site([self.page_with_resources([])],
                     "User-agent: *\nDisallow:\n\nUser-agent: Applebot\nDisallow: /\n")
        self.assertIn("robots.blocks_search_engine", found)
        self.assertIn("Apple", found["robots.blocks_search_engine"].evidence)
        self.assertEqual(found["robots.blocks_search_engine"].severity, Severity.CRITICAL)

    def test_blocking_an_ai_crawler_is_only_recorded(self):
        """Whether to allow AI crawlers is an editorial choice, not an SEO fault."""
        found = site([self.page_with_resources([])],
                     "User-agent: *\nDisallow:\n\nUser-agent: GPTBot\nDisallow: /\n")
        self.assertIn("robots.blocks_ai_crawlers", found)
        self.assertEqual(found["robots.blocks_ai_crawlers"].severity, Severity.INFO)
        self.assertNotIn("robots.blocks_search_engine", found)

    def test_qwant_is_recognised(self):
        found = site([self.page_with_resources([])],
                     "User-agent: *\nDisallow:\n\nUser-agent: Qwantbot\nDisallow: /\n")
        self.assertIn("Qwant", found["robots.blocks_search_engine"].evidence)

    def test_blocked_css_and_javascript_is_reported(self):
        """Google: don't block resources that make the page harder to understand.
        Apple asks for the same for Applebot."""
        page = self.page_with_resources([
            "https://e.test/assets/js/app.js", "https://e.test/css/site.css"])
        found = site([page], "User-agent: *\nDisallow: /assets/js/\n")
        self.assertIn("robots.blocks_resources", found)
        self.assertIn("app.js", found["robots.blocks_resources"].evidence)
        self.assertNotIn("site.css", found["robots.blocks_resources"].evidence)

    def test_crawlable_resources_are_not_reported(self):
        page = self.page_with_resources(["https://e.test/css/site.css"])
        self.assertNotIn("robots.blocks_resources",
                         site([page], "User-agent: *\nDisallow: /admin/\n"))

    def test_nothing_is_claimed_without_a_robots_file(self):
        page = self.page_with_resources(["https://e.test/a.js"])
        found = site([page], "")
        self.assertNotIn("robots.blocks_resources", found)
        self.assertNotIn("robots.blocks_search_engine", found)


class SingleModeParityTests(unittest.TestCase):
    """--html, --csv and --db were accepted in single mode and did nothing."""

    def test_single_mode_writes_every_requested_output(self):
        from seochecker.cli import main
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        body = ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                "<title>A page about handmade shoes from Guimaraes</title>"
                '<meta name="viewport" content="width=device-width, initial-scale=1">'
                "</head><body><h1>Shoes</h1><p>" + ("word " * 200) + "</p></body></html>")

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                payload = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        class Quiet(ThreadingHTTPServer):
            def handle_error(self, *a):
                pass

        server = Quiet(("127.0.0.1", 0), Handler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                out = Path(tmp)
                main([f"http://127.0.0.1:{port}/", "--single", "-q",
                      "-o", str(out / "r.json"), "--html", str(out / "r.html"),
                      "--csv", str(out / "r.csv")])
                self.assertTrue((out / "r.html").exists(), "--html was ignored")
                self.assertTrue((out / "r.csv").exists(), "--csv was ignored")
                import json
                report = json.loads((out / "r.json").read_text())
                self.assertIsNotNone(report["score"], "single mode produced no score")
                self.assertIn(report["score"]["grade"], list("ABCDEF"))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
