"""Phase 9 acceptance tests: Core Web Vitals and external data providers.

The thresholds here are Google's published ones, so these tests are also the
record of where the numbers came from.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seochecker.analyzers.vitals import LAB_CAVEAT, attach, findings_for
from seochecker.models import Page, Severity
from seochecker.providers import Moz, OpenPageRank, PageSpeedInsights, configured
from seochecker.providers.base import AuthorityProvider
from seochecker.render import playwright_available
from seochecker.vitals import THRESHOLDS, Vitals, measure, rate


class ThresholdTests(unittest.TestCase):
    """Google's published Core Web Vitals boundaries."""

    def test_the_three_core_vitals_use_googles_numbers(self):
        self.assertEqual(THRESHOLDS["lcp_ms"][0], 2500)      # good LCP
        self.assertEqual(THRESHOLDS["cls"][0], 0.1)          # good CLS
        # INP is deliberately absent: it cannot be measured without real users.
        self.assertNotIn("inp_ms", THRESHOLDS)

    def test_rating_boundaries(self):
        self.assertEqual(rate("lcp_ms", 2500), "good")
        self.assertEqual(rate("lcp_ms", 2501), "needs-improvement")
        self.assertEqual(rate("lcp_ms", 4001), "poor")
        self.assertEqual(rate("cls", 0.1), "good")
        self.assertEqual(rate("cls", 0.3), "poor")

    def test_nothing_measured_is_not_a_rating(self):
        self.assertEqual(rate("lcp_ms", None), "unknown")
        self.assertEqual(rate("made_up_metric", 1), "unknown")


class VitalsObjectTests(unittest.TestCase):
    def test_to_dict_works_on_a_slots_dataclass(self):
        """Vitals uses slots, so it has no __dict__ — this caught that."""
        data = Vitals(url="https://e.test/", lcp_ms=1234.56, cls=0.05).to_dict()
        self.assertEqual(data["lcp_ms"], 1234.6)
        self.assertEqual(data["source"], "lab")
        self.assertEqual(data["ratings"]["lcp_ms"], "good")

    def test_a_failed_measurement_is_not_ok(self):
        self.assertFalse(Vitals(url="https://e.test/", error="TimeoutError").ok)
        self.assertTrue(Vitals(url="https://e.test/", lcp_ms=100).ok)


def page_with(**vitals) -> Page:
    page = Page(requested_url="https://e.test/p", final_url="https://e.test/p",
                status=200, mime="text/html")
    page.vitals = Vitals(url=page.final_url, **vitals).to_dict()
    return page


class VitalsFindingTests(unittest.TestCase):
    def test_a_good_page_produces_nothing(self):
        self.assertEqual(findings_for(page_with(lcp_ms=1200, cls=0.02,
                                                ttfb_ms=200, fcp_ms=900,
                                                total_blocking_ms=50)), [])

    def test_poor_core_vitals_are_warnings(self):
        found = findings_for(page_with(lcp_ms=6000, cls=0.02, ttfb_ms=200,
                                       fcp_ms=900, total_blocking_ms=50))
        self.assertEqual([f.severity for f in found], [Severity.WARNING])
        self.assertIn("vitals.lcp", found[0].id)

    def test_needs_improvement_is_only_a_notice(self):
        found = findings_for(page_with(lcp_ms=3000, cls=0.02, ttfb_ms=200,
                                       fcp_ms=900, total_blocking_ms=50))
        self.assertEqual([f.severity for f in found], [Severity.NOTICE])

    def test_diagnostics_never_escalate_to_warnings(self):
        """TTFB and blocking time are diagnostics, not Core Web Vitals."""
        found = findings_for(page_with(lcp_ms=1200, cls=0.02, ttfb_ms=9000,
                                       fcp_ms=9000, total_blocking_ms=9000))
        self.assertTrue(found)
        self.assertEqual({f.severity for f in found}, {Severity.NOTICE})

    def test_every_finding_says_this_is_lab_data(self):
        found = findings_for(page_with(lcp_ms=6000, cls=0.4, ttfb_ms=200,
                                       fcp_ms=900, total_blocking_ms=50))
        self.assertTrue(found)
        for finding in found:
            with self.subTest(finding=finding.id):
                self.assertIn("not a substitute", finding.fix)

    def test_an_unmeasured_page_produces_nothing(self):
        page = Page(requested_url="https://e.test/", final_url="https://e.test/")
        self.assertEqual(findings_for(page), [])
        page.vitals = {"error": "TimeoutError"}
        self.assertEqual(findings_for(page), [])

    def test_attach_writes_onto_the_pages(self):
        pages = [page_with(lcp_ms=6000, cls=0.02, ttfb_ms=200, fcp_ms=900,
                           total_blocking_ms=50)]
        self.assertEqual(attach(pages), 1)
        self.assertEqual(len(pages[0].findings), 1)
        self.assertEqual(pages[0].findings[0].url, pages[0].final_url)


class ProviderTests(unittest.TestCase):
    def test_every_provider_satisfies_the_interface(self):
        for provider in configured():
            with self.subTest(provider=provider.name):
                self.assertIsInstance(provider, AuthorityProvider)

    def test_an_unconfigured_provider_says_what_it_needs(self):
        result = asyncio.run(PageSpeedInsights().page_metrics("https://e.test/"))
        self.assertFalse(result.ok)
        self.assertIn("Google API key", result.error)

    def test_paid_providers_are_marked_as_such(self):
        self.assertFalse(Moz().free)
        self.assertTrue(PageSpeedInsights().free)
        self.assertTrue(OpenPageRank().free)

    def test_a_key_makes_a_provider_available(self):
        self.assertFalse(OpenPageRank().available)
        self.assertTrue(OpenPageRank("a-key").available)

    def test_open_pagerank_is_domain_level_only(self):
        result = asyncio.run(OpenPageRank("k").page_metrics("https://e.test/page"))
        self.assertIn("domain-level", result.error)


@unittest.skipUnless(playwright_available(), "Playwright is an optional dependency")
class LiveMeasurementTests(unittest.TestCase):
    """Measured against a local page, so no network and no flakiness from the web."""

    @classmethod
    def setUpClass(cls):
        body = ("<!doctype html><html lang=en><head><meta charset=utf-8>"
                "<title>Measured</title></head><body>"
                "<h1>A heading large enough to be the largest contentful paint</h1>"
                "<p>" + ("word " * 400) + "</p></body></html>").encode()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        class Quiet(ThreadingHTTPServer):
            def handle_error(self, *a):
                pass

        cls.server = Quiet(("127.0.0.1", 0), Handler)
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def measure(self, **kwargs) -> Vitals:
        async def run():
            from playwright.async_api import async_playwright
            async with async_playwright() as pw:
                browser = await pw.chromium.launch()
                try:
                    return await measure(browser, self.url, **kwargs)
                finally:
                    await browser.close()
        return asyncio.run(run())

    def test_a_real_page_yields_real_numbers(self):
        vitals = self.measure(throttle=False)
        self.assertTrue(vitals.ok, vitals.error)
        self.assertGreater(vitals.lcp_ms, 0)
        self.assertIsNotNone(vitals.ttfb_ms)
        self.assertEqual(vitals.ratings()["lcp_ms"], "good")

    def test_an_unreachable_url_fails_as_data(self):
        async def run():
            from playwright.async_api import async_playwright
            async with async_playwright() as pw:
                browser = await pw.chromium.launch()
                try:
                    return await measure(browser, "http://127.0.0.1:9/nope", timeout=5)
                finally:
                    await browser.close()
        vitals = asyncio.run(run())
        self.assertFalse(vitals.ok)
        self.assertTrue(vitals.error)


if __name__ == "__main__":
    unittest.main(verbosity=2)
