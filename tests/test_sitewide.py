"""Phase 5 acceptance tests: whole-site analysis.

Two servers here — one that behaves correctly, and one that answers 200 for
everything, which is the only way to exercise soft-404 detection.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seochecker.analyzers import SiteContext, run_site_analyzers
from seochecker.cli import run_crawl
from seochecker.config import CrawlConfig
from seochecker.graph import LinkGraph
from seochecker.models import Page
from seochecker.similarity import content_hash, near_duplicate, similarity, sketch
from seochecker.thresholds import Thresholds

BODY = (
    "A oficina produz sapatos artesanais em Guimaraes desde mil novecentos e oitenta e sete. "
    "Cada par e cortado a mao a partir de couro curtido com taninos vegetais, cosido com linha "
    "encerada e montado sobre uma forma de madeira. O processo demora quarenta horas e envolve "
    "cinco artesaos, cada um responsavel por uma etapa distinta do fabrico completo do sapato."
)
PORT: dict[str, int] = {}


def doc(title: str, body: str, links=(), *, description="", canonical="", robots="") -> bytes:
    anchors = " ".join(f'<a href="{href}">link</a>' for href in links)
    head = f'<title>{title}</title><meta charset="utf-8">'
    if description:
        head += f'<meta name="description" content="{description}">'
    if canonical:
        head += f'<link rel="canonical" href="{canonical}">'
    if robots:
        head += f'<meta name="robots" content="{robots}">'
    return (f'<!doctype html><html lang="pt"><head>{head}</head>'
            f"<body><h1>{title}</h1><p>{body}</p>{anchors}</body></html>").encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, status, body=b"", ctype="text/html; charset=utf-8", extra=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for k, v in extra:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_HEAD(self):  # noqa: N802
        self.do_GET()

    def do_GET(self):  # noqa: N802
        path = self.path
        base = f"http://127.0.0.1:{PORT['port']}"

        if path == "/robots.txt":
            robots = f"User-agent: *\nDisallow:\n\nSitemap: {base}/sitemap.xml\n"
            return self._send(200, robots.encode(), "text/plain")
        if path == "/sitemap.xml":
            return self._send(200, (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                f"<url><loc>{base}/</loc></url>"
                f"<url><loc>{base}/orphan</loc></url>"
                "</urlset>").encode(), "application/xml")
        if path == "/":
            return self._send(200, doc("Home", BODY, [
                "/twin-a", "/twin-b", "/near", "/deep/1", "/broken", "/moved",
                "/canon-dupe", "/hidden",
                # Same server, different hostname — so the scope rules treat
                # these as external without needing a second machine.
                f"http://localhost:{PORT['port']}/external-ok",
                f"http://localhost:{PORT['port']}/external-gone",
            ], description="Home page of the workshop"))
        # Two pages, identical in every way that matters.
        if path in ("/twin-a", "/twin-b"):
            return self._send(200, doc("Sapato artesanal de couro", BODY, ["/"],
                                       description="Sapato artesanal de couro castanho"))
        # Same shape, one sentence different: a near-duplicate, not an exact one.
        if path == "/near":
            return self._send(200, doc("Bota artesanal de couro", BODY + " Preco: 240 euros.",
                                       ["/"], description="Bota artesanal"))
        # Declares itself a duplicate of /twin-a, so must not be reported as one.
        if path == "/canon-dupe":
            return self._send(200, doc("Sapato artesanal de couro", BODY, ["/"],
                                       description="Sapato artesanal de couro castanho",
                                       canonical=f"{base}/twin-a"))
        # noindex: also excluded from duplicate comparison.
        if path == "/hidden":
            return self._send(200, doc("Sapato artesanal de couro", BODY, ["/"],
                                       description="Sapato artesanal de couro castanho",
                                       robots="noindex"))
        if path == "/orphan":
            return self._send(200, doc("Orfa", BODY + " Esta pagina existe apenas no sitemap.",
                                       [], description="Pagina orfa"))
        if path == "/moved":
            return self._send(301, b"", extra=[("Location", "/twin-a")])
        if path == "/broken":
            return self._send(404, doc("Gone", "not here"))
        if path.startswith("/deep/"):
            depth = int(path.rsplit("/", 1)[1])
            links = [f"/deep/{depth + 1}"] if depth < 6 else []
            return self._send(200, doc(f"Deep {depth}", BODY + f" nivel {depth}", links))
        if path == "/external-ok":
            return self._send(200, doc("External", BODY))
        if path == "/external-gone":
            return self._send(404, doc("External gone", "no"))
        return self._send(404, doc("Not found", "no such page"))


class SoftHandler(Handler):
    """A site that answers 200 for everything, including URLs that do not exist."""

    def do_GET(self):  # noqa: N802
        if self.path in ("/robots.txt", "/sitemap.xml"):
            return self._send(404, b"", "text/plain")
        if self.path == "/":
            return self._send(200, doc("Home", BODY, ["/real"]))
        if self.path == "/real":
            return self._send(200, doc("Real", BODY, ["/"]))
        return self._send(200, doc("Pagina nao encontrada",
                                   "Lamentamos mas a pagina que procura nao existe neste site. "
                                   "Volte a pagina inicial ou use a pesquisa para encontrar o "
                                   "que precisa no nosso catalogo de produtos artesanais."))


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass


def serve(handler) -> tuple[QuietServer, int]:
    server = QuietServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


class SiteWideTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.port = serve(Handler)
        PORT["port"] = cls.port
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.result = cls.crawl_once(cls.base)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    @staticmethod
    def crawl_once(base, **overrides):
        settings = dict(url=base + "/", http2=False, delay=0.0, jitter=0.0, timeout=5.0,
                        max_retries=0, concurrency=4, max_pages=40, max_depth=8)
        settings.update(overrides)
        return asyncio.run(run_crawl(CrawlConfig(**settings)))

    def ids(self, result=None) -> set[str]:
        return {f.id for f in (result or self.result).site_findings}

    def finding(self, finding_id: str):
        return next(f for f in self.result.site_findings if f.id == finding_id)

    # --- duplicates --------------------------------------------------------

    def test_duplicate_title_description_and_h1(self):
        found = self.ids()
        for check in ("duplicate.title", "duplicate.description", "duplicate.h1"):
            self.assertIn(check, found)

    def test_identical_bodies_are_reported(self):
        self.assertIn("duplicate.content", self.ids())

    def test_near_duplicate_is_reported_separately(self):
        self.assertIn("duplicate.near_content", self.ids())

    def test_canonicalised_duplicate_is_not_reported(self):
        """/canon-dupe declares itself a copy of /twin-a. That is the fix, not the fault."""
        self.assertNotIn("/canon-dupe", self.finding("duplicate.title").evidence)

    def test_noindexed_duplicate_is_not_reported(self):
        self.assertNotIn("/hidden", self.finding("duplicate.title").evidence)

    # --- structure ---------------------------------------------------------

    def test_orphan_page_is_reported(self):
        self.assertIn("structure.orphan_pages", self.ids())

    def test_broken_internal_link_names_its_source(self):
        finding = self.finding("structure.broken_internal_links")
        self.assertIn("/broken", finding.evidence)
        self.assertIn("linked from", finding.evidence)

    def test_link_to_a_redirect_is_reported(self):
        self.assertIn("structure.links_to_redirects", self.ids())

    def test_deep_pages_are_reported(self):
        self.assertIn("structure.deep_pages", self.ids())

    def test_pagerank_is_reported(self):
        self.assertIn("structure.internal_pagerank", self.ids())

    def test_correct_404s_produce_no_soft_404_finding(self):
        self.assertNotIn("structure.soft_404", self.ids())

    # --- graph -------------------------------------------------------------

    def test_graph_metrics_land_on_the_pages(self):
        home = next(p for p in self.result.pages if p.requested_url.rstrip("/") == self.base)
        self.assertGreater(home.pagerank, 0)
        self.assertEqual(home.click_depth, 0)
        self.assertGreater(home.inlink_count, 0)

    def test_pagerank_is_a_distribution(self):
        total = sum(self.result.graph.pagerank.values())
        self.assertAlmostEqual(total, 1.0, places=4)


class ExternalLinkTests(unittest.TestCase):
    """--check-external is opt-in because it sends requests to other people's servers."""

    @classmethod
    def setUpClass(cls):
        cls.server, cls.port = serve(Handler)
        PORT["port"] = cls.port
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_external_links_are_not_checked_by_default(self):
        result = SiteWideTests.crawl_once(self.base)
        self.assertEqual(result.external_links, {})
        self.assertNotIn("external.broken_links", {f.id for f in result.site_findings})

    def test_broken_external_link_is_found_when_asked(self):
        result = SiteWideTests.crawl_once(self.base, check_external=True)
        self.assertTrue(result.external_links)
        finding = next(f for f in result.site_findings if f.id == "external.broken_links")
        self.assertIn("/external-gone", finding.evidence)
        self.assertIn("linked from", finding.evidence)

    def test_working_external_link_is_not_reported(self):
        result = SiteWideTests.crawl_once(self.base, check_external=True)
        finding = next(f for f in result.site_findings if f.id == "external.broken_links")
        self.assertNotIn("/external-ok", finding.evidence)


class SoftFourOhFourTests(unittest.TestCase):
    def test_site_returning_200_for_everything_is_caught(self):
        server, port = serve(SoftHandler)
        PORT["port"] = port
        try:
            result = SiteWideTests.crawl_once(f"http://127.0.0.1:{port}")
            self.assertIn("structure.soft_404", {f.id for f in result.site_findings})
            self.assertTrue(result.soft_404_fingerprint)
        finally:
            server.shutdown()
            server.server_close()


class GraphTests(unittest.TestCase):
    def graph(self):
        def page(url, links):
            p = Page(requested_url=url, final_url=url, status=200, mime="text/html")
            p.outlinks = links
            return p

        base = "https://e.test"
        self.pages = [
            page(f"{base}/", [f"{base}/a", f"{base}/b"]),
            page(f"{base}/a", [f"{base}/", f"{base}/b"]),
            page(f"{base}/b", [f"{base}/"]),
            page(f"{base}/c", []),
        ]
        return LinkGraph.build(self.pages, f"{base}/")

    def test_more_inlinks_ranks_higher(self):
        graph = self.graph()
        self.assertGreater(graph.pagerank["https://e.test/b"], graph.pagerank["https://e.test/a"])

    def test_orphans_and_unreachable(self):
        graph = self.graph()
        self.assertEqual(graph.orphans(), ["https://e.test/c"])
        self.assertEqual(graph.unreachable(), ["https://e.test/c"])

    def test_click_depth_uses_links_not_crawl_order(self):
        graph = self.graph()
        self.assertEqual(graph.click_depth["https://e.test/"], 0)
        self.assertEqual(graph.click_depth["https://e.test/a"], 1)
        self.assertNotIn("https://e.test/c", graph.click_depth)


UNRELATED = ("Receita de bacalhau a bras com batata palha cebola ovos azeitonas salsa "
             "picada azeite alho pimenta preta vinagre balsamico salada verde pao quente "
             "servido a mesa com vinho branco fresco da regiao dos vinhos verdes")


class SimilarityTests(unittest.TestCase):
    def test_thresholds_discriminate(self):
        base = sketch(BODY)
        self.assertTrue(near_duplicate(base, sketch(BODY)))
        self.assertTrue(near_duplicate(base, sketch(BODY + " Preco: 240 euros.")))
        self.assertFalse(near_duplicate(base, sketch(UNRELATED)))

    def test_similarity_does_not_depend_on_document_length(self):
        """The reason SimHash was replaced: a fixed threshold meant different
        things on short and long documents."""
        edit = " Preco: 240 euros."
        short_score = similarity(sketch(BODY), sketch(BODY + edit))
        long_score = similarity(sketch(BODY * 3), sketch(BODY * 3 + edit))
        self.assertAlmostEqual(short_score, long_score, delta=0.05)
        self.assertGreater(short_score, 0.9)

    def test_similarity_is_a_readable_proportion(self):
        self.assertEqual(similarity(sketch(BODY), sketch(BODY)), 1.0)
        self.assertLess(similarity(sketch(BODY), sketch(UNRELATED)), 0.1)

    def test_exact_hash_ignores_whitespace_and_case(self):
        self.assertEqual(content_hash(BODY), content_hash("  " + BODY.upper() + "  "))

    def test_short_text_is_not_fingerprinted(self):
        self.assertEqual(sketch("Pagina inicial"), ())
        self.assertEqual(content_hash("Pagina inicial"), "")
        self.assertFalse(near_duplicate((), ()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
