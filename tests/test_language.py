"""Language-aware duplicate detection.

Google's rule, which these tests encode: "Localized versions of a page are only
considered duplicates if the main content of the page remains untranslated."

So two hreflang alternates sharing a brand-name title are not a duplicate
problem, while two alternates serving the same untranslated body text are — and
they are a different problem with a different fix, so they get a different
finding.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seochecker import language
from seochecker.cli import run_crawl
from seochecker.config import CrawlConfig
from seochecker.models import Page

PORT: dict[str, int] = {}

ENGLISH = ("The workshop has made shoes by hand in Guimaraes since nineteen eighty seven. "
           "Every pair is cut from leather tanned with vegetable tannins, stitched with waxed "
           "thread and lasted over a wooden form by five different craftspeople. ")
PORTUGUESE = ("A oficina produz sapatos a mao em Guimaraes desde mil novecentos e oitenta e "
              "sete. Cada par e cortado em couro curtido com taninos vegetais, cosido com linha "
              "encerada e montado sobre uma forma de madeira por cinco artesaos diferentes. ")


def build(title: str, body: str, *, lang: str, alternates: list[tuple[str, str]],
          heading: str) -> bytes:
    links = "".join(
        f'<link rel="alternate" hreflang="{code}" href="http://127.0.0.1:{PORT["port"]}{path}">'
        for code, path in alternates
    )
    nav = "".join(f'<a href="{path}">{code}</a> ' for code, path in alternates)
    return (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8">'
            f"<title>{title}</title>"
            f'<meta name="description" content="{title} — {heading}">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f"{links}</head><body><h1>{heading}</h1><p>{body * 3}</p>{nav}"
            f'<a href="/dup-a">a</a> <a href="/dup-b">b</a></body></html>').encode()


HOME_ALTS = [("en", "/en/"), ("pt", "/pt/")]
ABOUT_ALTS = [("en", "/en/about"), ("pt", "/pt/sobre")]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, body, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path
        if path in ("/robots.txt", "/sitemap.xml"):
            return self._send(b"", 404)

        # Properly translated alternates that share a brand-name title and H1.
        if path == "/en/":
            return self._send(build("Sapatos Guimaraes", ENGLISH, lang="en",
                                    alternates=HOME_ALTS, heading="Sapatos Guimaraes"))
        if path == "/pt/":
            return self._send(build("Sapatos Guimaraes", PORTUGUESE, lang="pt",
                                    alternates=HOME_ALTS, heading="Sapatos Guimaraes"))

        # Alternates whose body was never translated.
        if path in ("/en/about", "/pt/sobre"):
            lang = "en" if path.startswith("/en") else "pt"
            return self._send(build("About the workshop", ENGLISH, lang=lang,
                                    alternates=ABOUT_ALTS, heading="About the workshop"))

        # Plain duplicates with no language relationship at all.
        if path in ("/dup-a", "/dup-b"):
            return self._send(build("Catalogue of handmade shoes", ENGLISH, lang="en",
                                    alternates=[], heading="Catalogue"))

        if path == "/":
            body = (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
                    f"<title>Index of the workshop site</title>"
                    f'<meta name="viewport" content="width=device-width, initial-scale=1">'
                    f"</head><body><h1>Index</h1><p>{ENGLISH * 2}</p>"
                    f'<a href="/en/">en</a> <a href="/pt/">pt</a> '
                    f'<a href="/en/about">about</a> <a href="/pt/sobre">sobre</a> '
                    f'<a href="/dup-a">a</a> <a href="/dup-b">b</a></body></html>').encode()
            return self._send(body)
        return self._send(b"<html><body>nope</body></html>", 404)


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass


class ClusterTests(unittest.TestCase):
    """Cluster building, without needing a crawl."""

    @staticmethod
    def page(url: str, alternates: list[list[str]]) -> Page:
        page = Page(requested_url=url, final_url=url, status=200, mime="text/html")
        page.seo = {"hreflang": alternates, "lang": "xx"}
        return page

    def test_reciprocal_alternates_form_a_cluster(self):
        en, pt = "https://e.test/en/", "https://e.test/pt/"
        alts = [["en", en], ["pt", pt]]
        clusters = language.build([self.page(en, alts), self.page(pt, alts)])
        self.assertTrue(clusters.same_cluster(en, pt))

    def test_a_one_way_declaration_does_not(self):
        """Google ignores hreflang that is not reciprocal, so we do too."""
        en, pt = "https://e.test/en/", "https://e.test/pt/"
        clusters = language.build([self.page(en, [["pt", pt]]), self.page(pt, [])])
        self.assertFalse(clusters.same_cluster(en, pt))

    def test_an_uncrawled_alternate_is_given_the_benefit_of_the_doubt(self):
        """Reciprocity can't be checked against a page the budget never reached,
        so the declaration is trusted — and the page is not penalised for it."""
        en, de = "https://e.test/en/", "https://e.test/de/"
        pages = [self.page(en, [["en", en], ["de", de]])]
        clusters = language.build(pages)
        self.assertIsNotNone(clusters.cluster_for(en))
        # It is alone among crawled pages, so nothing is collapsed away.
        self.assertEqual(len(clusters.collapse(pages)), 1)

    def test_pages_naming_the_same_alternate_are_treated_as_one_set(self):
        """hreflang sets are meant to be complete, so this transitivity is
        deliberate: two pages both claiming /de/ as an alternate are claiming
        membership of the same cluster."""
        en, us, de = "https://e.test/en/", "https://e.test/en-us/", "https://e.test/de/"
        pages = [self.page(en, [["de", de]]), self.page(us, [["de", de]])]
        clusters = language.build(pages)
        self.assertTrue(clusters.same_cluster(en, us))

    def test_x_default_does_not_link_pages(self):
        en, pt = "https://e.test/en/", "https://e.test/pt/"
        clusters = language.build([
            self.page(en, [["x-default", pt]]), self.page(pt, [["x-default", en]])])
        self.assertFalse(clusters.same_cluster(en, pt))

    def test_collapse_leaves_one_page_per_cluster(self):
        en, pt, fr = "https://e.test/en/", "https://e.test/pt/", "https://e.test/fr/"
        alts = [["en", en], ["pt", pt], ["fr", fr]]
        pages = [self.page(en, alts), self.page(pt, alts), self.page(fr, alts)]
        clusters = language.build(pages)
        self.assertEqual(len(clusters.collapse(pages)), 1)

    def test_unrelated_pages_are_never_collapsed(self):
        pages = [self.page("https://e.test/a", []), self.page("https://e.test/b", [])]
        clusters = language.build(pages)
        self.assertEqual(len(clusters.collapse(pages)), 2)


class LiveDuplicateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = QuietServer(("127.0.0.1", 0), Handler)
        PORT["port"] = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{PORT['port']}/"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.result = asyncio.run(run_crawl(CrawlConfig(
            url=cls.base, max_pages=20, http2=False, delay=0.0, jitter=0.0, timeout=5.0,
            max_retries=0, concurrency=3, use_sitemap=False, probe_soft_404=False,
            quiet=True, render="never")))
        cls.findings = {f.id: f for f in cls.result.site_findings}

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_translated_alternates_sharing_a_title_are_not_reported(self):
        """/en/ and /pt/ share a brand-name title. A brand does not get translated."""
        finding = self.findings.get("duplicate.title")
        if finding is not None:
            self.assertNotIn("Sapatos Guimaraes", finding.evidence)

    def test_translated_alternates_sharing_an_h1_are_not_reported(self):
        finding = self.findings.get("duplicate.h1")
        if finding is not None:
            self.assertNotIn("Sapatos Guimaraes", finding.evidence)

    def test_genuine_duplicates_are_still_reported(self):
        """The fix must not silence duplicates that have nothing to do with language."""
        self.assertIn("duplicate.title", self.findings)
        self.assertIn("Catalogue", self.findings["duplicate.title"].evidence)

    def test_untranslated_alternates_are_reported_as_their_own_problem(self):
        self.assertIn("duplicate.untranslated_localizations", self.findings)
        evidence = self.findings["duplicate.untranslated_localizations"].evidence
        self.assertIn("/about", evidence)
        self.assertIn("/sobre", evidence)

    def test_untranslated_alternates_are_not_reported_as_plain_duplicates(self):
        finding = self.findings.get("duplicate.content")
        if finding is not None:
            self.assertNotIn("/sobre", finding.evidence)

    def test_the_advice_explains_why(self):
        fix = self.findings["duplicate.untranslated_localizations"].fix
        self.assertIn("untranslated", fix.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
