"""Phase 11 acceptance tests: comparison against rival sites.

Two fixture sites with deliberately different characteristics — one thin and
plain, one richer with structured data — so the gaps the comparison reports can
be checked against what the fixtures actually contain.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seochecker.cli import run_comparison
from seochecker.compare import MEANINGFUL_GAP, Comparison, SiteMetrics, format_metric
from seochecker.config import CrawlConfig
from seochecker.fingerprint import Detection

PROSE = ("A oficina produz sapatos artesanais em Guimaraes desde mil novecentos e oitenta e "
         "sete, com couro curtido a taninos vegetais e solas cosidas a mao. ")

SCHEMA = ('<script type="application/ld+json">{"@context":"https://schema.org",'
          '"@type":"Product","name":"Sapato","image":"https://e.test/p.jpg",'
          '"offers":{"@type":"Offer","price":"120","priceCurrency":"EUR"}}</script>')


def page(title: str, *, words: int, links: str, extra_head: str = "",
         description: str = "") -> bytes:
    meta = f'<meta name="description" content="{description}">' if description else ""
    return (f'<!doctype html><html lang="pt"><head><meta charset="utf-8">'
            f"<title>{title}</title>{meta}{extra_head}"
            f'<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
            f"<body><h1>{title}</h1><p>{PROSE * words}</p>{links}</body></html>").encode()


def make_handler(rich: bool):
    """rich=True is the stronger site: more content, descriptions, schema markup."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_GET(self):  # noqa: N802
            if self.path in ("/robots.txt", "/sitemap.xml"):
                body, status, ctype = b"", 404, "text/plain"
            elif self.path in ("/", "/a", "/b"):
                links = '<a href="/">home</a> <a href="/a">a</a> <a href="/b">b</a>'
                body = page(
                    f"Pagina {self.path} da oficina de sapatos artesanais",
                    words=12 if rich else 2,
                    links=links,
                    extra_head=SCHEMA if rich else "",
                    description=("Sapatos artesanais feitos a mao em Portugal com couro "
                                 "curtido e solas cosidas." if rich else ""),
                )
                status, ctype = 200, "text/html"
            else:
                body, status, ctype = page("Nao encontrado", words=1, links=""), 404, "text/html"
            self.send_response(status)
            self.send_header("Content-Type", f"{ctype}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)

    return Handler


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass


def serve(rich: bool):
    server = QuietServer(("127.0.0.1", 0), make_handler(rich))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/"


def metrics(**overrides) -> SiteMetrics:
    base = dict(url="https://e.test/", host="e.test", budget=10, pages_crawled=5,
                html_pages=5, score=70.0, grade="C")
    base.update(overrides)
    return SiteMetrics(**base)


class GapTests(unittest.TestCase):
    """Gap logic, without needing a crawl."""

    def compare(self, target: SiteMetrics, *rivals: SiteMetrics) -> Comparison:
        return Comparison(target=target, rivals=list(rivals), budget=10)

    def test_technology_a_rival_has_and_we_do_not(self):
        comparison = self.compare(
            metrics(technologies={"cms": ["WordPress"]}),
            metrics(host="rival.test", technologies={"cms": ["WordPress"],
                                                     "analytics": ["Hotjar"]}),
        )
        gaps = comparison.technology_gaps()
        self.assertEqual([g.label for g in gaps], ["Hotjar"])
        self.assertEqual(gaps[0].rivals, ["rival.test"])

    def test_technology_we_have_and_they_do_not_is_not_a_gap(self):
        comparison = self.compare(
            metrics(technologies={"analytics": ["Hotjar"]}),
            metrics(host="rival.test", technologies={}),
        )
        self.assertEqual(comparison.technology_gaps(), [])

    def test_schema_gap_names_every_rival_using_it(self):
        comparison = self.compare(
            metrics(schema_types=["Organization"]),
            metrics(host="a.test", schema_types=["Organization", "FAQPage"]),
            metrics(host="b.test", schema_types=["FAQPage", "Product"]),
        )
        gaps = {g.label: g.rivals for g in comparison.schema_gaps()}
        self.assertEqual(gaps["FAQPage"], ["a.test", "b.test"])
        self.assertEqual(gaps["Product"], ["b.test"])

    def test_a_small_score_difference_is_not_a_gap(self):
        comparison = self.compare(
            metrics(category_scores={"content": 80.0}),
            metrics(host="rival.test", category_scores={"content": 80.0 + MEANINGFUL_GAP - 1}),
        )
        self.assertEqual(comparison.category_gaps(), [])

    def test_a_real_score_difference_is_reported_once_with_the_best_rival(self):
        comparison = self.compare(
            metrics(category_scores={"content": 50.0}),
            metrics(host="a.test", category_scores={"content": 70.0}),
            metrics(host="b.test", category_scores={"content": 90.0}),
        )
        gaps = comparison.category_gaps()
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0].rivals, ["b.test"])
        self.assertIn("50 vs 90", gaps[0].detail)

    def test_strengths_need_to_beat_every_rival(self):
        ahead = self.compare(
            metrics(category_scores={"technical": 95.0}),
            metrics(host="a.test", category_scores={"technical": 60.0}),
            metrics(host="b.test", category_scores={"technical": 70.0}),
        )
        self.assertEqual([g.label for g in ahead.strengths()], ["technical"])

        mixed = self.compare(
            metrics(category_scores={"technical": 95.0}),
            metrics(host="a.test", category_scores={"technical": 99.0}),
        )
        self.assertEqual(mixed.strengths(), [])

    def test_best_value_depends_on_which_direction_is_good(self):
        comparison = self.compare(
            metrics(median_words=900.0, median_ttfb_ms=50.0),
            metrics(host="rival.test", median_words=100.0, median_ttfb_ms=900.0),
        )
        rows = {row["label"]: row for row in comparison.metric_table()}
        # More words is better, so the target wins that one...
        self.assertTrue(rows["Median words per page"]["cells"][0]["best"])
        self.assertFalse(rows["Median words per page"]["cells"][1]["best"])
        # ...and a lower server response time is better, so the target wins that too.
        self.assertTrue(rows["Median server response, ms"]["cells"][0]["best"])
        self.assertFalse(rows["Median server response, ms"]["cells"][1]["best"])

    def test_identical_values_mark_nobody_as_best(self):
        comparison = self.compare(metrics(median_words=500.0),
                                  metrics(host="rival.test", median_words=500.0))
        row = next(r for r in comparison.metric_table()
                   if r["label"] == "Median words per page")
        self.assertFalse(any(cell["best"] for cell in row["cells"]))

    def test_the_output_states_its_limits(self):
        payload = self.compare(metrics(), metrics(host="rival.test")).to_dict()
        self.assertEqual(payload["budget"], 10)
        for phrase in ("backlinks", "traffic", "rankings"):
            self.assertIn(phrase, payload["disclaimer"])

    def test_formatting(self):
        self.assertEqual(format_metric(0.25, "pct"), "25%")
        self.assertEqual(format_metric(12.0, "int"), "12")
        self.assertEqual(format_metric(12.5, "num"), "12.5")


class LiveComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plain_server, cls.plain = serve(rich=False)
        cls.rich_server, cls.rich = serve(rich=True)

    @classmethod
    def tearDownClass(cls):
        for server in (cls.plain_server, cls.rich_server):
            server.shutdown()
            server.server_close()

    def compare(self, target: str, rival: str, budget: int = 6) -> Comparison:
        config = CrawlConfig(url=target, against=[rival], max_pages=budget, http2=False,
                             delay=0.0, jitter=0.0, timeout=5.0, max_retries=0,
                             concurrency=2, use_sitemap=False, probe_soft_404=False,
                             quiet=True, render="never")
        _, _, comparison = asyncio.run(run_comparison(config))
        return comparison

    def test_every_site_gets_the_same_budget(self):
        comparison = self.compare(self.plain, self.rich, budget=6)
        self.assertEqual(comparison.budget, 6)
        for site in comparison.sites:
            self.assertLessEqual(site.pages_crawled, 6)
            self.assertEqual(site.budget, 6)

    def test_the_richer_site_measures_richer(self):
        comparison = self.compare(self.plain, self.rich)
        target, rival = comparison.target, comparison.rivals[0]
        self.assertLess(target.median_words, rival.median_words)
        self.assertLess(target.structured_data_share, rival.structured_data_share)
        self.assertGreater(target.missing_description_share, rival.missing_description_share)

    def test_the_schema_gap_is_found(self):
        comparison = self.compare(self.plain, self.rich)
        self.assertIn("Product", [g.label for g in comparison.schema_gaps()])

    def test_comparing_the_other_way_round_reverses_the_gaps(self):
        comparison = self.compare(self.rich, self.plain)
        self.assertEqual(comparison.schema_gaps(), [])
        self.assertTrue(comparison.strengths() or not comparison.category_gaps())

    def test_hosts_are_distinguishable(self):
        comparison = self.compare(self.plain, self.rich)
        hosts = [site.host for site in comparison.sites]
        self.assertEqual(len(set(hosts)), 2)
        self.assertTrue(all(hosts))


if __name__ == "__main__":
    unittest.main(verbosity=2)
