"""Phase 4 acceptance tests: the crawl engine.

All of it runs against a throwaway server on localhost, so scope, robots and
sitemap behaviour can be asserted exactly rather than inferred from a live site.
"""

from __future__ import annotations

import asyncio
import re
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seochecker.cli import run_crawl
from seochecker.config import CrawlConfig
from seochecker.frontier import Frontier
from seochecker.robots import parse as parse_robots, product_token
from seochecker.sitemap import parse_sitemap
from seochecker.urls import Scope, normalize

PORT_HOLDER: dict[str, int] = {}


def page(title: str, links: list[str] = (), extra: str = "") -> bytes:
    anchors = " ".join(f'<a href="{href}">{href}</a>' for href in links)
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        f"<title>{title}</title>{extra}</head>"
        f"<body><h1>{title}</h1>{anchors}</body></html>"
    ).encode()


ROBOTS = b"""User-agent: *
Disallow: /admin/
Crawl-delay: 0

Sitemap: /sitemap.xml
"""


def sitemap_xml(port: int) -> bytes:
    base = f"http://127.0.0.1:{port}"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>{base}/</loc></url>"
        f"<url><loc>{base}/orphan</loc></url>"
        f"<url><loc>{base}/orphan2</loc></url>"
        f"<url><loc>{base}/orphan3</loc></url>"
        f"<url><loc>{base}/orphan4</loc></url>"
        f"<url><loc>{base}/gone</loc></url>"
        "</urlset>"
    ).encode()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hits: list[str] = []

    def log_message(self, *args):
        pass

    def _send(self, status, body=b"", ctype="text/html; charset=utf-8", extra=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for key, value in extra:
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path
        Handler.hits.append(path)
        port = PORT_HOLDER["port"]

        if path == "/robots.txt":
            return self._send(200, ROBOTS, "text/plain")
        if path == "/sitemap.xml":
            return self._send(200, sitemap_xml(port), "application/xml")
        if path == "/":
            return self._send(200, page("Home", [
                "/a", "/b", "/admin/secret", "/a?utm_source=news", "/a#frag",
                "https://elsewhere.test/x", "/deep/1",
            ]))
        if path.startswith("/a"):
            return self._send(200, page("A", ["/", "/b"]))
        if path == "/b":
            return self._send(200, page("B", ["/c"]))
        if path == "/c":
            return self._send(200, page("C", []))
        if path.startswith("/orphan"):
            return self._send(200, page("Orphan", []))
        if path == "/admin/secret":
            return self._send(200, page("Secret", []))
        if path == "/gone":
            return self._send(404, page("Gone", []))
        if match := re.fullmatch(r"/deep/(\d+)", path):
            depth = int(match.group(1))
            return self._send(200, page(f"Deep {depth}", [f"/deep/{depth + 1}"]))
        if path == "/moved":
            return self._send(301, b"", extra=[("Location", "/c")])
        return self._send(404, page("Not found"))


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass


class CrawlTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = QuietServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        PORT_HOLDER["port"] = cls.port
        cls.base = f"http://127.0.0.1:{cls.port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def crawl(self, **overrides):
        Handler.hits = []
        settings = dict(url=self.base + "/", http2=False, delay=0.0, jitter=0.0,
                        timeout=5.0, max_retries=0, concurrency=4, max_pages=50, max_depth=5)
        settings.update(overrides)
        return asyncio.run(run_crawl(CrawlConfig(**settings)))

    def paths(self, result) -> set[str]:
        from urllib.parse import urlsplit
        return {urlsplit(p.requested_url).path for p in result.pages}

    # --- discovery ---------------------------------------------------------

    def test_follows_internal_links(self):
        found = self.paths(self.crawl())
        self.assertLessEqual({"/", "/a", "/b", "/c"}, found)

    def test_sitemap_seeds_unlinked_pages(self):
        """/orphan is in the sitemap and linked from nowhere."""
        result = self.crawl()
        self.assertIn("/orphan", self.paths(result))
        self.assertNotIn("/orphan", self.paths(self.crawl(use_sitemap=False)))
        orphan = next(p for p in result.pages if p.requested_url.endswith("/orphan"))
        self.assertTrue(orphan.from_sitemap, "sitemap origin should be recorded on the page")

    def test_link_discovery_is_not_starved_by_the_sitemap(self):
        """A small budget must still leave room to follow links.

        Seeding the whole sitemap first means nothing past depth 0 is ever
        reached, and the link graph is never seen at all.
        """
        result = self.crawl(max_pages=4)
        linked = [p for p in result.pages if p.depth >= 1]
        self.assertTrue(
            linked,
            "no page beyond depth 0 was crawled — the sitemap consumed the whole budget",
        )

    def test_offsite_links_are_not_followed(self):
        result = self.crawl()
        hosts = {p.requested_url.split("/")[2] for p in result.pages}
        self.assertEqual(hosts, {f"127.0.0.1:{self.port}"})
        self.assertIn("off-site", result.frontier["skipped"])

    # --- deduplication -----------------------------------------------------

    def test_tracking_params_and_fragments_collapse(self):
        """/a, /a?utm_source=news and /a#frag are one page, fetched once."""
        result = self.crawl()
        a_pages = [p for p in result.pages if p.requested_url.endswith("/a")]
        self.assertEqual(len(a_pages), 1)
        self.assertEqual(len([h for h in Handler.hits if h.startswith("/a")]), 1)

    # --- limits ------------------------------------------------------------

    def test_max_pages_is_respected(self):
        result = self.crawl(max_pages=3)
        self.assertLessEqual(len(result.pages), 3)
        self.assertIn("page limit", result.stopped_because)

    def test_max_depth_is_respected(self):
        result = self.crawl(max_depth=2, use_sitemap=False)
        self.assertIn("/deep/1", self.paths(result))
        self.assertNotIn("/deep/4", self.paths(result))
        self.assertIn("beyond max depth", result.frontier["skipped"])

    # --- robots.txt --------------------------------------------------------

    def test_robots_disallow_is_obeyed(self):
        result = self.crawl()
        self.assertNotIn("/admin/secret", self.paths(result))
        self.assertNotIn("/admin/secret", Handler.hits)
        self.assertIn("blocked by robots.txt", result.frontier["skipped"])

    def test_ignore_robots_crawls_the_disallowed_path(self):
        result = self.crawl(obey_robots=False)
        self.assertIn("/admin/secret", self.paths(result))

    def test_robots_sitemap_line_is_used(self):
        result = self.crawl()
        self.assertTrue(result.robots.fetched)
        self.assertTrue(any(s.endswith("/sitemap.xml") for s in result.robots.sitemaps))

    # --- site findings -----------------------------------------------------

    def test_sitemap_entry_that_404s_is_reported(self):
        ids = {f.id for f in self.crawl().site_findings}
        self.assertIn("sitemap.broken_entries", ids)

    def test_scope_patterns(self):
        result = self.crawl(exclude_patterns=[re.compile(r"/deep/")], use_sitemap=False)
        self.assertNotIn("/deep/1", self.paths(result))
        self.assertIn("excluded by pattern", result.frontier["skipped"])


class NormalizationTests(unittest.TestCase):
    def test_equivalent_urls_collapse(self):
        same = [
            "https://Example.com/A/?utm_source=x&b=2&a=1#frag",
            "https://example.com:443/A/?a=1&b=2&fbclid=zz",
            "https://EXAMPLE.COM/A/?b=2&a=1",
        ]
        self.assertEqual(len({normalize(u) for u in same}), 1)

    def test_distinct_urls_stay_distinct(self):
        different = ["https://e.test/a", "https://e.test/a/", "https://e.test/A",
                     "https://e.test/a?page=2", "http://e.test/a"]
        self.assertEqual(len({normalize(u) for u in different}), len(different))

    def test_session_id_is_stripped_from_the_path(self):
        self.assertEqual(normalize("https://e.test/p;jsessionid=ABC?x=1"),
                         "https://e.test/p?x=1")


class ScopeTests(unittest.TestCase):
    def test_www_and_bare_host_are_one_site(self):
        scope = Scope("https://example.com/")
        self.assertTrue(scope.allows("https://www.example.com/a"))

    def test_subdomains_need_opting_in(self):
        self.assertFalse(Scope("https://example.com/").allows("https://blog.example.com/a"))
        self.assertTrue(Scope("https://example.com/", include_subdomains=True)
                        .allows("https://blog.example.com/a"))

    def test_lookalike_domain_is_rejected(self):
        """The suffix match must be anchored on a dot."""
        scope = Scope("https://example.com/", include_subdomains=True)
        self.assertFalse(scope.allows("https://evil-example.com/a"))
        self.assertFalse(scope.allows("https://example.com.evil.test/a"))


class RobotsTests(unittest.TestCase):
    SAMPLE = """
User-agent: *
Disallow: /admin/
Allow: /admin/public/
Disallow: /*.pdf$
Crawl-delay: 2

User-agent: seochecker
Disallow: /private/
"""

    def setUp(self):
        self.robots = parse_robots(self.SAMPLE, source_url="https://e.test/robots.txt")

    def test_longest_match_wins(self):
        self.assertFalse(self.robots.is_allowed("https://e.test/admin/x", "googlebot"))
        self.assertTrue(self.robots.is_allowed("https://e.test/admin/public/x", "googlebot"))

    def test_dollar_anchors_the_end(self):
        self.assertFalse(self.robots.is_allowed("https://e.test/f.pdf", "googlebot"))
        self.assertTrue(self.robots.is_allowed("https://e.test/f.pdf.html", "googlebot"))

    def test_specific_group_replaces_the_wildcard_group(self):
        self.assertTrue(self.robots.is_allowed("https://e.test/admin/x", "seochecker"))
        self.assertFalse(self.robots.is_allowed("https://e.test/private/x", "seochecker"))

    def test_crawl_delay(self):
        self.assertEqual(self.robots.crawl_delay("googlebot"), 2.0)

    def test_empty_disallow_allows_everything(self):
        robots = parse_robots("User-agent: *\nDisallow:")
        self.assertTrue(robots.is_allowed("https://e.test/anything", "seochecker"))

    def test_product_token_extraction(self):
        self.assertEqual(
            product_token("Mozilla/5.0 (compatible; seochecker/0.1.0; +https://x)"),
            "seochecker")


class FrontierTests(unittest.TestCase):
    def frontier(self, **kwargs):
        return Frontier(scope=Scope("https://e.test/"), **kwargs)

    def test_duplicates_are_accepted_once(self):
        frontier = self.frontier()
        self.assertIsNotNone(frontier.consider("https://e.test/a", 0))
        self.assertIsNone(frontier.consider("https://e.test/a?utm_source=x", 0))
        self.assertEqual(frontier.skipped["already seen"], 1)

    def test_page_limit_stops_acceptance(self):
        frontier = self.frontier(max_pages=2)
        for index in range(5):
            frontier.consider(f"https://e.test/{index}", 0)
        self.assertEqual(frontier.accepted, 2)
        self.assertTrue(frontier.full)


if __name__ == "__main__":
    unittest.main(verbosity=2)
