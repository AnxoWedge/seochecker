"""Phase 8 acceptance tests: not getting blocked.

The point of this phase is that the crawler stops asking when a host says stop,
and that re-running an audit costs the site almost nothing. Both are things a
server operator would care about, so both are asserted against what the server
actually received.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seochecker.cache import ResponseCache
from seochecker.cli import run_crawl
from seochecker.config import CrawlConfig
from seochecker.fetch import (
    BACKOFF_FACTOR, MAX_HOST_DELAY, RECOVERY_AFTER, SLOW_HOST_MS, HostHealth,
)

LINKS = " ".join(f'<a href="/p{i}">p{i}</a>' for i in range(30))
BODY = ("A oficina produz sapatos artesanais em Guimaraes desde mil novecentos e oitenta e sete. "
        "Cada par e cortado a mao a partir de couro curtido com taninos vegetais. ")


def doc(title: str, links: str = "") -> bytes:
    return (f'<!doctype html><html lang="pt"><head><meta charset="utf-8"><title>{title}</title>'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
            f"<body><h1>{title}</h1><p>{BODY * 4}</p>{links}</body></html>").encode()


class Handler(BaseHTTPRequestHandler):
    """A healthy site with cache validators."""

    protocol_version = "HTTP/1.1"
    hits: list[str] = []
    conditional_hits: list[str] = []
    ETAG = '"v1"'

    def log_message(self, *a):
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

    def do_GET(self):  # noqa: N802
        Handler.hits.append(self.path)
        if self.path in ("/robots.txt", "/sitemap.xml"):
            return self._send(404, b"", "text/plain")
        if self.request_version and self.headers.get("If-None-Match") == Handler.ETAG:
            Handler.conditional_hits.append(self.path)
            self.send_response(304)
            self.send_header("ETag", Handler.ETAG)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        pages = {"/": doc("Home", '<a href="/a">a</a> <a href="/b">b</a>'),
                 "/a": doc("A", '<a href="/">home</a>'),
                 "/b": doc("B", '<a href="/">home</a>')}
        if self.path in pages:
            return self._send(200, pages[self.path], extra=[("ETag", Handler.ETAG)])
        return self._send(404, doc("Missing"))


class RateLimiter(BaseHTTPRequestHandler):
    """A host that rate-limits everything except its home page."""

    protocol_version = "HTTP/1.1"
    hits: list[str] = []

    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        RateLimiter.hits.append(self.path)
        if self.path == "/robots.txt":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        status = 200 if self.path == "/" else 429
        body = doc("Home" if status == 200 else "Slow down", LINKS)
        self.send_response(status)
        if status == 429:
            self.send_header("Retry-After", "0")
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass


def serve(handler):
    server = QuietServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


class HostHealthTests(unittest.TestCase):
    def test_blocks_back_off_exponentially(self):
        health = HostHealth.start(0.25)
        health.record_block(max_blocks=99, reason="429")
        first = health.delay
        health.record_block(max_blocks=99, reason="429")
        self.assertAlmostEqual(health.delay, first * BACKOFF_FACTOR, places=4)
        self.assertGreater(health.delay, 0.25)

    def test_backoff_is_capped(self):
        health = HostHealth.start(1.0)
        for _ in range(50):
            health.record_block(max_blocks=1000, reason="429")
        self.assertLessEqual(health.delay, MAX_HOST_DELAY)

    def test_circuit_trips_after_repeated_blocks(self):
        health = HostHealth.start(0.1)
        for _ in range(2):
            health.record_block(max_blocks=3, reason="429")
        self.assertFalse(health.tripped)
        health.record_block(max_blocks=3, reason="429")
        self.assertTrue(health.tripped)
        self.assertIn("429", health.trip_reason)

    def test_a_success_clears_the_block_streak(self):
        health = HostHealth.start(0.1)
        health.record_block(max_blocks=3, reason="429")
        health.record_block(max_blocks=3, reason="429")
        health.record_success(100.0)
        health.record_block(max_blocks=3, reason="429")
        self.assertFalse(health.tripped, "the streak should have reset on success")

    def test_recovery_is_slower_than_backoff(self):
        health = HostHealth.start(0.25)
        health.record_block(max_blocks=99, reason="429")
        health.record_block(max_blocks=99, reason="429")
        peak = health.delay
        for _ in range(RECOVERY_AFTER):
            health.record_success(50.0)
        self.assertLess(health.delay, peak)
        self.assertGreaterEqual(health.delay, 0.25)

    def test_a_slow_host_gets_more_room_unprompted(self):
        health = HostHealth.start(0.25)
        for _ in range(6):
            health.record_success(SLOW_HOST_MS + 1000)
        self.assertGreater(health.delay, 0.25)

    def test_robots_crawl_delay_can_only_slow_us_down(self):
        health = HostHealth.start(2.0)
        health.set_base(0.5)
        self.assertEqual(health.base_delay, 2.0)
        health.set_base(5.0)
        self.assertEqual(health.base_delay, 5.0)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "cache.sqlite3"

    def tearDown(self):
        self.dir.cleanup()

    def test_round_trip(self):
        with ResponseCache(self.path) as cache:
            cache.store("https://e.test/", final_url="https://e.test/", status=200,
                        headers={"etag": '"a"'}, body=b"<html>hi</html>")
            entry = cache.get("https://e.test/")
        self.assertEqual(entry.body, b"<html>hi</html>")
        self.assertEqual(entry.conditional_headers(), {"If-None-Match": '"a"'})

    def test_freshness_window(self):
        with ResponseCache(self.path, ttl=60) as cache:
            cache.store("https://e.test/", final_url="https://e.test/", status=200,
                        headers={}, body=b"x")
            entry = cache.get("https://e.test/")
        self.assertTrue(entry.is_fresh(60))
        self.assertFalse(entry.is_fresh(0))

    def test_what_must_not_be_cached(self):
        with ResponseCache(self.path) as cache:
            self.assertFalse(cache.store("https://e.test/a", final_url="a", status=500,
                                         headers={}, body=b"x"))
            self.assertFalse(cache.store("https://e.test/b", final_url="b", status=429,
                                         headers={}, body=b"x"))
            self.assertFalse(cache.store("https://e.test/c", final_url="c", status=200,
                                         headers={"cache-control": "no-store"}, body=b"x"))
            self.assertTrue(cache.store("https://e.test/d", final_url="d", status=200,
                                        headers={}, body=b"x"))


class CrawlPolitenessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.port = serve(Handler)
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def crawl(self, **overrides):
        settings = dict(url=self.base + "/", http2=False, delay=0.0, jitter=0.0, timeout=5.0,
                        max_retries=0, concurrency=2, max_pages=10, use_sitemap=False,
                        probe_soft_404=False, quiet=True, render="never")
        settings.update(overrides)
        return asyncio.run(run_crawl(CrawlConfig(**settings)))

    def test_a_warm_cache_barely_touches_the_server(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = str(Path(tmp) / "c.sqlite3")
            Handler.hits = []
            first = self.crawl(cache=cache)
            cold = len(Handler.hits)

            Handler.hits = []
            second = self.crawl(cache=cache)
            warm = len(Handler.hits)

        self.assertEqual(len(first.pages), len(second.pages))
        self.assertGreater(cold, 0)
        self.assertLess(warm, cold, "the second run should ask the server for far less")
        self.assertGreaterEqual(second.stats["cache"]["hits"], 1)

    def test_stale_entries_are_revalidated_not_re_downloaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = str(Path(tmp) / "c.sqlite3")
            self.crawl(cache=cache)
            Handler.conditional_hits = []
            result = self.crawl(cache=cache, cache_ttl=0.0)
        self.assertTrue(Handler.conditional_hits, "no conditional requests were sent")
        self.assertGreaterEqual(result.stats["cache"]["revalidated"], 1)

    def test_a_healthy_site_never_trips_the_breaker(self):
        result = self.crawl(max_blocks=3)
        self.assertEqual(result.blocked_hosts, {})


class CircuitBreakerTests(unittest.TestCase):
    def test_the_crawler_stops_asking_a_host_that_says_stop(self):
        RateLimiter.hits = []
        server, port = serve(RateLimiter)
        try:
            result = asyncio.run(run_crawl(CrawlConfig(
                url=f"http://127.0.0.1:{port}/", http2=False, delay=0.0, jitter=0.0,
                timeout=5.0, max_retries=0, concurrency=1, max_pages=40, max_blocks=3,
                use_sitemap=False, probe_soft_404=False, quiet=True, render="never")))
        finally:
            server.shutdown()
            server.server_close()

        # 30 links plus home and robots.txt were all discoverable; almost none
        # should have been requested.
        self.assertLess(len(RateLimiter.hits), 8,
                        f"kept asking a rate-limiting host: {len(RateLimiter.hits)} requests")
        self.assertTrue(result.blocked_hosts)
        self.assertIn("consecutive blocks", next(iter(result.blocked_hosts.values())))
        self.assertIn("stopped asking", result.stopped_because)


if __name__ == "__main__":
    unittest.main(verbosity=2)
