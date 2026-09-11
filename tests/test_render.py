"""Phase 6 acceptance tests: JavaScript rendering.

The heuristic is tested without a browser. The rendering tests need Chromium and
are skipped when Playwright is absent, since it is an optional dependency. They
share one crawl, because launching a browser costs more than every other test in
this suite put together.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seochecker.cli import run_crawl
from seochecker.config import CrawlConfig
from seochecker.html import Document
from seochecker.render import RENDER_WORD_THRESHOLD, playwright_available, should_render

PROSE = "A oficina produz sapatos artesanais em Guimaraes desde mil novecentos e oitenta e sete. "

SHELL = """<!doctype html><html lang="pt"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body><div id="root"></div>
<script src="/a.js"></script><script src="/b.js"></script><script src="/c.js"></script>
<script>
document.title = "Titulo posto por JavaScript";
var m = document.createElement("meta");
m.name = "description";
m.content = "Descricao que so existe depois do JavaScript correr no navegador.";
document.head.appendChild(m);
document.getElementById("root").innerHTML =
  '<h1>Sapatos</h1><p>' + "%s".repeat(6) + '</p>' +
  '<a href="/real-a">A</a> <a href="/real-b">B</a>';
window.Shopify = {}; window.jQuery = function(){};
</script></body></html>""" % PROSE


def plain(title: str) -> bytes:
    return (f'<!doctype html><html lang="pt"><head><meta charset="utf-8">'
            f'<title>{title} da oficina de sapatos artesanais</title>'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
            f'<body><h1>{title}</h1><p>{PROSE * 8}</p><a href="/">Inicio</a></body></html>').encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, status, body=b"", ctype="text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path in ("/robots.txt", "/sitemap.xml"):
            return self._send(404, b"", "text/plain")
        if self.path == "/":
            return self._send(200, SHELL.encode())
        if self.path.endswith(".js"):
            return self._send(200, b"/* stub */", "application/javascript")
        if self.path in ("/real-a", "/real-b"):
            return self._send(200, plain(self.path.strip("/")))
        return self._send(404, plain("Nao encontrado"))


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass


class HeuristicTests(unittest.TestCase):
    """No browser needed: this is the decision, not the rendering."""

    def decide(self, html: str) -> str:
        return should_render(Document(html, "https://e.test/"))

    def test_empty_app_shell_is_rendered(self):
        self.assertIn("no readable text",
                      self.decide('<html><body><div id="root"></div></body></html>'))

    def test_thin_page_behind_many_scripts_is_rendered(self):
        html = "<html><body><p>Ola mundo.</p>" + '<script src="/a.js"></script>' * 6 + "</body></html>"
        self.assertIn("6 scripts", self.decide(html))

    def test_thin_page_with_a_shell_container_is_rendered(self):
        html = '<html><body><div id="app"><p>Ola mundo.</p></div></body></html>'
        self.assertIn("app shell", self.decide(html))

    def test_a_real_page_is_left_alone(self):
        html = f"<html><body><p>{'palavra ' * (RENDER_WORD_THRESHOLD + 50)}</p></body></html>"
        self.assertEqual(self.decide(html), "")

    def test_thin_page_without_shell_or_scripts_is_left_alone(self):
        """Short pages exist. Only render when something suggests content is hidden."""
        html = "<html><body><p>Uma pagina curta mas completa.</p></body></html>"
        self.assertEqual(self.decide(html), "")

    def test_no_document_means_no_render(self):
        self.assertEqual(should_render(None), "")


@unittest.skipUnless(playwright_available(), "Playwright is an optional dependency")
class RenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = QuietServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        # One rendered crawl, shared: launching Chromium is the expensive part.
        cls.rendered = cls.crawl(render="auto")
        cls.plain = cls.crawl(render="never")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    @classmethod
    def crawl(cls, **overrides):
        settings = dict(url=cls.base + "/", http2=False, delay=0.0, jitter=0.0, timeout=20.0,
                        max_retries=0, concurrency=2, max_pages=10, use_sitemap=False,
                        probe_soft_404=False)
        settings.update(overrides)
        return asyncio.run(run_crawl(CrawlConfig(**settings)))

    def home(self, result):
        return next(p for p in result.pages if p.requested_url.rstrip("/") == self.base)

    def ids(self, page) -> set[str]:
        return {f.id for f in page.findings}

    # --- the headline case -------------------------------------------------

    def test_without_rendering_the_spa_is_a_dead_end(self):
        self.assertEqual(len(self.plain.pages), 1)
        self.assertIn("content.empty", self.ids(self.home(self.plain)))

    def test_rendering_reveals_the_links_and_the_crawl_continues(self):
        paths = {p.requested_url.replace(self.base, "") for p in self.rendered.pages}
        self.assertEqual(paths, {"/", "/real-a", "/real-b"})

    def test_rendered_page_is_analysed_as_rendered(self):
        found = self.ids(self.home(self.rendered))
        self.assertNotIn("content.empty", found)
        self.assertNotIn("title.missing", found)

    # --- the findings ------------------------------------------------------

    def test_render_findings(self):
        found = self.ids(self.home(self.rendered))
        for check in ("render.content_requires_js", "render.links_require_js",
                      "render.title_requires_js", "render.description_requires_js"):
            with self.subTest(finding=check):
                self.assertIn(check, found)

    def test_diff_records_both_sides(self):
        diff = self.home(self.rendered).render_diff
        self.assertEqual(diff["words_before"], 0)
        self.assertGreater(diff["words_after"], 50)
        self.assertEqual(diff["links_before"], 0)
        self.assertEqual(diff["links_after"], 2)

    # --- what rendering is for beyond content ------------------------------

    def test_js_globals_feed_the_fingerprint(self):
        home = self.home(self.rendered)
        self.assertIn("Shopify", home.js_globals)
        self.assertIn("Shopify", {t.name for t in self.rendered.technologies})

    def test_only_pages_that_need_it_are_rendered(self):
        """/real-a and /real-b serve real HTML, so they must not cost a render."""
        self.assertEqual(self.rendered.stats["rendered"], 1)
        self.assertTrue(self.home(self.rendered).rendered)
        self.assertFalse(any(p.rendered for p in self.rendered.pages
                             if p.requested_url != self.home(self.rendered).requested_url))

    def test_render_cap_is_respected(self):
        result = self.crawl(render="always", max_render=1)
        self.assertEqual(result.stats["rendered"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
