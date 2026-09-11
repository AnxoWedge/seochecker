"""Phase 1 acceptance tests: a bad network must never take the crawl down.

Everything runs against a throwaway HTTP server on localhost, so the suite is
deterministic and makes no outside requests.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from seochecker.config import CrawlConfig, normalize_target
from seochecker.fetch import (
    Fetcher,
    classify_exception,
    decode_body,
    parse_retry_after,
    split_content_type,
)
from seochecker.html import Document
from seochecker.models import ErrorKind

HTML_OK = b"""<!doctype html><html lang="en"><head><title>OK</title>
<meta name="description" content="A test page."><link rel="canonical" href="/ok">
</head><body><h1>Hello</h1><p>Some words here.</p>
<img src="a.png" alt="a"><img src="b.png"></body></html>"""

LATIN = "<!doctype html><meta charset=iso-8859-1><title>Ol\xe1</title><p>Ac\xe7\xe3o".encode("iso-8859-1")


class QuietServer(ThreadingHTTPServer):
    """Truncation tests deliberately hang up mid-body; don't log the reset."""

    def handle_error(self, request, client_address):
        pass


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    flaky_hits = 0

    def log_message(self, *args):  # keep the test output clean
        pass

    def _send(self, status, body=b"", headers=()):
        self.send_response(status)
        for key, value in headers:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path
        if path == "/ok":
            return self._send(200, HTML_OK, [("Content-Type", "text/html; charset=utf-8")])
        if path == "/404":
            return self._send(404, b"<h1>Not found</h1>", [("Content-Type", "text/html")])
        if path == "/r1":
            return self._send(302, headers=[("Location", "/r2")])
        if path == "/r2":
            return self._send(301, headers=[("Location", "/ok")])
        if path == "/loop":
            return self._send(302, headers=[("Location", "/loop2")])
        if path == "/loop2":
            return self._send(302, headers=[("Location", "/loop")])
        if path.startswith("/chain/"):
            n = int(path.rsplit("/", 1)[1])
            return self._send(302, headers=[("Location", f"/chain/{n + 1}")])
        if path == "/500":
            return self._send(500, b"boom", [("Content-Type", "text/plain")])
        if path == "/flaky":
            Handler.flaky_hits += 1
            if Handler.flaky_hits == 1:
                return self._send(503, b"busy", [("Retry-After", "0"),
                                                 ("Content-Type", "text/plain")])
            return self._send(200, HTML_OK, [("Content-Type", "text/html")])
        if path == "/slow":
            time.sleep(2.0)
            return self._send(200, HTML_OK, [("Content-Type", "text/html")])
        if path == "/image":
            return self._send(200, b"\x89PNG\r\n\x1a\n" + b"x" * 5000,
                              [("Content-Type", "image/png")])
        if path == "/latin":
            return self._send(200, LATIN, [("Content-Type", "text/html")])
        if path == "/big":
            return self._send(200, b"<html><body>" + b"y" * 200_000,
                              [("Content-Type", "text/html")])
        if path == "/blocked":
            return self._send(403, b"<html><head><title>Just a moment...</title></head></html>",
                              [("Content-Type", "text/html"), ("Server", "cloudflare")])
        return self._send(404, b"nope", [("Content-Type", "text/plain")])


class FetchTests(unittest.IsolatedAsyncioTestCase):
    server: ThreadingHTTPServer
    base: str

    @classmethod
    def setUpClass(cls):
        cls.server = QuietServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def config(self, **overrides) -> CrawlConfig:
        base = dict(url=self.base + "/ok", http2=False, delay=0.0, jitter=0.0,
                    timeout=5.0, max_retries=1)
        base.update(overrides)
        return CrawlConfig(**base)

    async def get(self, path: str, **overrides):
        config = self.config(**overrides)
        async with Fetcher(config) as fetcher:
            return await fetcher.fetch(self.base + path)

    # --- happy path --------------------------------------------------------

    async def test_ok(self):
        page = await self.get("/ok")
        self.assertTrue(page.ok)
        self.assertEqual(page.status, 200)
        self.assertEqual(page.mime, "text/html")
        self.assertTrue(page.is_html)
        self.assertIn("Hello", page.html)
        self.assertEqual(page.requests, 1)
        self.assertEqual(page.retries, 0)
        self.assertIsNotNone(page.timing.ttfb_ms)
        self.assertGreater(page.wire_bytes, 0)

    async def test_404_is_recorded_not_raised(self):
        page = await self.get("/404")
        self.assertFalse(page.ok)
        self.assertEqual(page.status, 404)
        self.assertIsNone(page.error)

    # --- redirects ---------------------------------------------------------

    async def test_redirect_chain_is_captured(self):
        page = await self.get("/r1")
        self.assertTrue(page.ok)
        self.assertEqual(page.final_url, self.base + "/ok")
        self.assertEqual([hop.status for hop in page.redirects], [302, 301])
        self.assertEqual(page.requests, 3)

    async def test_redirect_loop(self):
        page = await self.get("/loop")
        self.assertEqual(page.error, ErrorKind.REDIRECT_LOOP)

    async def test_too_many_redirects(self):
        page = await self.get("/chain/1", max_redirects=3)
        self.assertEqual(page.error, ErrorKind.TOO_MANY_REDIRECTS)

    # --- retries -----------------------------------------------------------

    async def test_500_retried_then_reported(self):
        page = await self.get("/500", max_retries=2)
        self.assertEqual(page.status, 500)
        self.assertEqual(page.requests, 3)
        self.assertEqual(page.retries, 2)

    async def test_503_with_retry_after_recovers(self):
        Handler.flaky_hits = 0
        page = await self.get("/flaky", max_retries=2)
        self.assertTrue(page.ok)
        self.assertEqual(page.retries, 1)

    # --- failures ----------------------------------------------------------

    async def test_timeout_becomes_an_error_not_an_exception(self):
        page = await self.get("/slow", timeout=0.5, max_retries=0)
        self.assertEqual(page.error, ErrorKind.TIMEOUT)
        self.assertIsNotNone(page.timing.total_ms)

    async def test_connection_refused(self):
        config = self.config(max_retries=0)
        async with Fetcher(config) as fetcher:
            page = await fetcher.fetch("http://127.0.0.1:1/nothing")
        self.assertEqual(page.error, ErrorKind.CONNECTION)

    async def test_tls_error_classification(self):
        import ssl
        try:
            raise ssl.SSLCertVerificationError("certificate has expired")
        except ssl.SSLError as inner:
            try:
                raise httpx.ConnectError("failed") from inner
            except httpx.ConnectError as outer:
                kind, detail = classify_exception(outer)
        self.assertEqual(kind, ErrorKind.TLS)
        self.assertIn("expired", detail)

    async def test_bot_mitigation_is_reported(self):
        page = await self.get("/blocked", max_retries=0)
        self.assertEqual(page.error, ErrorKind.BLOCKED)
        self.assertIn("cloudflare", page.error_detail)

    # --- bodies ------------------------------------------------------------

    async def test_non_text_body_is_not_downloaded(self):
        page = await self.get("/image")
        self.assertEqual(page.mime, "image/png")
        self.assertIsNone(page.html)
        self.assertFalse(page.is_html)

    async def test_meta_charset_is_honoured(self):
        page = await self.get("/latin")
        self.assertEqual(page.charset_source, "meta")
        self.assertIn("Ol\xe1", page.html)
        self.assertIn("Ac\xe7\xe3o", page.html)

    async def test_oversized_body_is_truncated(self):
        page = await self.get("/big", max_bytes=10_000)
        self.assertTrue(page.truncated)
        self.assertLessEqual(len(page.html.encode()), 10_000)


class HelperTests(unittest.TestCase):
    def test_split_content_type(self):
        self.assertEqual(split_content_type('text/HTML; charset="UTF-8"'), ("text/html", "utf-8"))
        self.assertEqual(split_content_type("image/png"), ("image/png", ""))

    def test_retry_after(self):
        self.assertEqual(parse_retry_after("30"), 30.0)
        self.assertIsNone(parse_retry_after("not a date"))
        self.assertIsNone(parse_retry_after(""))

    def test_decode_precedence(self):
        self.assertEqual(decode_body(b"\xef\xbb\xbfhi")[2], "bom")
        self.assertEqual(decode_body("olá".encode(), "utf-8")[2], "header")
        self.assertEqual(decode_body(b"<meta charset=iso-8859-1>caf\xe9")[2], "meta")
        self.assertEqual(decode_body(b"plain")[2], "default")
        self.assertEqual(decode_body(b"caf\xe9")[2], "fallback")

    def test_normalize_target(self):
        self.assertEqual(normalize_target("example.com"), "https://example.com/")
        self.assertEqual(normalize_target("http://a.test/x#y"), "http://a.test/x")
        with self.assertRaises(ValueError):
            normalize_target("ftp://a.test")

    def test_svg_titles_are_not_page_titles(self):
        doc = Document(
            "<html><head><title>Real</title></head><body>"
            '<svg><title>icon</title></svg><svg><title>icon2</title></svg>'
            "</body></html>",
            "https://a.test/",
        )
        self.assertEqual(doc.titles, ["Real"])

    def test_alt_absent_versus_empty(self):
        doc = Document(
            '<img src="1.png" alt="x"><img src="2.png"><img src="3.png" alt="">',
            "https://a.test/",
        )
        self.assertEqual([img.has_alt for img in doc.images], [True, False, True])
        self.assertEqual([img.decorative for img in doc.images], [False, False, True])


if __name__ == "__main__":
    unittest.main(verbosity=2)
