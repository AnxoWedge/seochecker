"""Being framework-aware: finding what a build hides, and not blaming it for its shape.

Both halves come from one live Next.js site. Its Google Analytics was invisible
because the measurement id lives in a JavaScript chunk rather than the HTML, and
its text-to-HTML ratio looked catastrophic because half the document is a
hydration payload.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seochecker.analyzers import PageContext, run_page_analyzers
from seochecker.cli import run_crawl
from seochecker.config import CrawlConfig
from seochecker.fingerprint import Fingerprinter
from seochecker.fingerprint.detect import _compile
from seochecker.html import Document
from seochecker.models import Page

PROSE = ("A oficina produz sapatos artesanais em Guimaraes desde mil novecentos e "
         "oitenta e sete, com couro curtido a taninos vegetais. ")


def analyse(html: str) -> set[str]:
    page = Page(requested_url="https://e.test/p", final_url="https://e.test/p",
                status=200, mime="text/html",
                headers={"content-type": "text/html"}, html=html)
    page.timing.ttfb_ms = 100
    return {f.id for f in run_page_analyzers(PageContext(
        page=page, doc=Document(html, page.final_url),
        config=CrawlConfig(url=page.final_url)))}


class IdentifierMatchingTests(unittest.TestCase):
    """Measurement ids are uppercase. Matching them loosely invents detections."""

    def test_a_ga4_id_is_matched_case_sensitively(self):
        pattern = _compile(r"G-[A-Z0-9]{8,}", True)
        self.assertTrue(pattern.search("G-Y8V7N7RTQL"))
        # A minified bundle is full of things like this.
        self.assertFalse(pattern.search("g-searchParams"))
        self.assertFalse(pattern.search("g-somelowercasething"))

    def test_patterns_are_case_insensitive_unless_asked(self):
        self.assertTrue(_compile("wp-content").search("WP-CONTENT"))
        self.assertFalse(_compile("wp-content", True).search("WP-CONTENT"))


class BundleFingerprintTests(unittest.TestCase):
    def detect(self, bundles: str = "", html: str = "<html><body>x</body></html>"):
        page = Page(requested_url="https://e.test/", final_url="https://e.test/",
                    status=200, mime="text/html", headers={}, html=html)
        page.bundles = bundles
        return {d.name for d in Fingerprinter().detect(page, Document(html, page.final_url))}

    def test_analytics_hidden_in_a_javascript_chunk_is_found(self):
        found = self.detect(bundles='var id="G-Y8V7N7RTQL";'
                                    'src="https://www.googletagmanager.com/gtag/js"')
        self.assertIn("Google Analytics 4", found)

    def test_a_lowercase_lookalike_in_a_bundle_is_not_analytics(self):
        self.assertNotIn("Google Analytics 4",
                         self.detect(bundles="const g-searchParams = new URLSearchParams()"))

    def test_nothing_is_claimed_when_no_bundle_was_read(self):
        self.assertNotIn("Google Analytics 4", self.detect(bundles=""))

    def test_a_consent_api_in_a_bundle_counts(self):
        self.assertIn("Cookie consent banner", self.detect(bundles="window.__tcfapi=function(){}"))


class MarkupMeasurementTests(unittest.TestCase):
    def test_embedded_script_and_style_are_not_counted_as_markup(self):
        payload = "x" * 50_000
        doc = Document(f"<html><body><p>hello</p><script>{payload}</script>"
                       f"<style>{payload}</style></body></html>", "https://e.test/")
        self.assertLess(doc.markup_bytes, 5_000)
        self.assertGreater(doc.embedded_bytes, 90_000)

    def test_a_page_with_no_embedded_data_reports_none(self):
        doc = Document("<html><body><p>hello</p></body></html>", "https://e.test/")
        self.assertEqual(doc.embedded_bytes, 0)


class TextRatioTests(unittest.TestCase):
    """The ratio only means something when the page is also short of words."""

    NESTED = "<div class='a b c'><span class='d e f'>" * 90 + "</span></div>" * 90

    def test_a_thin_page_buried_in_markup_is_reported(self):
        self.assertIn("content.low_text_ratio",
                      analyse(f"<html lang=en><body>{self.NESTED}"
                              "<p>Only a few words here.</p></body></html>"))

    def test_the_same_markup_around_real_content_is_not(self):
        """A component framework nests deeply. That is not a fault by itself."""
        self.assertNotIn("content.low_text_ratio",
                         analyse(f"<html lang=en><body>{self.NESTED}"
                                 f"<p>{PROSE * 40}</p></body></html>"))

    def test_a_hydration_payload_does_not_count_against_the_page(self):
        self.assertNotIn("content.low_text_ratio",
                         analyse("<html lang=en><body><p>" + PROSE * 40 + "</p><script>"
                                 + ("x" * 200_000) + "</script></body></html>"))


class LiveBundleScanTests(unittest.TestCase):
    """End to end: a page whose only analytics marker is inside its own script."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), cls.handler())
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}/"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    @staticmethod
    def handler():
        page = (f'<!doctype html><html lang="pt"><head><meta charset="utf-8">'
                f"<title>A oficina de sapatos artesanais de Guimaraes</title>"
                f'<meta name="viewport" content="width=device-width, initial-scale=1">'
                f'</head><body><h1>Oficina</h1><p>{PROSE * 6}</p>'
                f'<script src="/chunks/a.js"></script>'
                f'<script src="/chunks/layout.js"></script></body></html>').encode()
        # The measurement id lives here and nowhere else, as on a real build.
        layout = (b'var gtagId="G-Y8V7N7RTQL";'
                  b'loadScript("https://www.googletagmanager.com/gtag/js?id="+gtagId);')

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                if self.path in ("/robots.txt", "/sitemap.xml"):
                    body, status, ctype = b"", 404, "text/plain"
                elif self.path == "/chunks/layout.js":
                    body, status, ctype = layout, 200, "application/javascript"
                elif self.path.startswith("/chunks/"):
                    body, status, ctype = b"var noop=1;", 200, "application/javascript"
                elif self.path == "/":
                    body, status, ctype = page, 200, "text/html"
                else:
                    body, status, ctype = b"missing", 404, "text/html"
                self.send_response(status)
                self.send_header("Content-Type", f"{ctype}; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

        return Handler

    def crawl(self, **overrides):
        settings = dict(url=self.base, http2=False, delay=0.0, jitter=0.0, timeout=5.0,
                        max_retries=0, concurrency=2, max_pages=3, use_sitemap=False,
                        probe_soft_404=False, quiet=True, render="never")
        settings.update(overrides)
        return asyncio.run(run_crawl(CrawlConfig(**settings)))

    def test_analytics_in_a_chunk_is_detected(self):
        result = self.crawl()
        self.assertIn("Google Analytics 4", {t.name for t in result.technologies})

    def test_and_is_missed_when_bundle_scanning_is_off(self):
        """Confirms the detection really comes from reading the JavaScript."""
        result = self.crawl(scan_bundles=False)
        self.assertNotIn("Google Analytics 4", {t.name for t in result.technologies})

    def test_no_analytics_is_not_reported_when_it_was_found(self):
        result = self.crawl()
        ids = {f.id for f in result.site_findings}
        self.assertNotIn("technical.no_analytics", ids)
        self.assertNotIn("technical.analytics_behind_consent", ids)


if __name__ == "__main__":
    unittest.main(verbosity=2)
