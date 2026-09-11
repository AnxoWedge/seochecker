"""Phase 2 acceptance tests.

The most valuable test here is `test_clean_page_is_quiet`: a well-built page must
produce no criticals and no warnings. A checker that cries wolf on a good page is
worse than no checker at all.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seochecker.analyzers import PageContext, run_page_analyzers
from seochecker.config import CrawlConfig
from seochecker.html import Document
from seochecker.models import ErrorKind, Page, Severity
from seochecker.thresholds import Thresholds

URL = "https://ex.test/loja/sapatos"

GOOD_HEADERS = {
    "content-type": "text/html; charset=utf-8",
    "content-encoding": "br",
    "strict-transport-security": "max-age=63072000",
    "etag": '"abc123"',
    "server": "nginx",
}

FILLER = " ".join(["Vendemos sapatos artesanais feitos em Portugal com couro curtido."] * 45)

CLEAN = f"""<!doctype html><html lang="pt-PT"><head>
<meta charset="utf-8">
<title>Sapatos artesanais portugueses feitos a mao | Loja</title>
<meta name="description" content="Sapatos artesanais feitos a mao em Portugal, com couro
curtido naturalmente e solas cosidas. Envio gratuito para todo o pais em 48 horas.">
<link rel="canonical" href="{URL}">
<link rel="icon" href="/favicon.ico">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta property="og:title" content="Sapatos artesanais portugueses">
<meta property="og:description" content="Feitos a mao em Portugal.">
<meta property="og:image" content="https://ex.test/og.jpg">
<meta property="og:url" content="{URL}">
<meta property="og:type" content="website">
<meta name="twitter:card" content="summary_large_image">
<script type="application/ld+json">
{{"@context":"https://schema.org","@type":"Product","name":"Sapato Porto",
"image":"https://ex.test/p.jpg","offers":{{"@type":"Offer","price":"120.00","priceCurrency":"EUR"}}}}
</script>
<script src="/app.js" defer></script>
</head><body>
<h1>Sapatos artesanais portugueses</h1>
<h2>Como sao feitos</h2><p>{FILLER}</p>
<h2>Envios</h2><p>{FILLER}</p>
<img src="/s1.jpg" alt="Sapato castanho de couro" width="600" height="400">
<img src="/s2.jpg" alt="Sola cosida a mao" width="600" height="400">
<a href="/loja/botas">ver a colecao de botas</a>
<a href="/sobre-nos">a historia da oficina</a>
</body></html>"""


def analyze(html: str | None, *, url: str = URL, status: int = 200,
            headers: dict[str, str] | None = None, error: ErrorKind | None = None,
            ttfb: float = 120.0) -> list:
    page = Page(
        requested_url=url,
        final_url=url,
        status=status,
        reason="OK",
        http_version="HTTP/2",
        headers={**GOOD_HEADERS, **(headers or {})},
        mime="text/html",
        charset="utf-8",
        charset_source="header",
        html=html,
        decoded_bytes=len(html or ""),
        error=error,
        error_detail="challenge" if error else "",
    )
    page.timing.ttfb_ms = ttfb
    doc = Document(html, url) if html is not None and error is None else None
    return run_page_analyzers(
        PageContext(page=page, doc=doc, config=CrawlConfig(url=url), thresholds=Thresholds())
    )


def ids(html: str | None, **kwargs) -> set[str]:
    return {f.id for f in analyze(html, **kwargs)}


class CleanPageTests(unittest.TestCase):
    def test_clean_page_is_quiet(self):
        findings = analyze(CLEAN)
        loud = [f for f in findings
                if f.severity in (Severity.CRITICAL, Severity.WARNING)]
        self.assertEqual(loud, [], f"false positives: {[(f.id, f.message) for f in loud]}")

    def test_clean_page_still_reports_its_structured_data(self):
        self.assertIn("structured.present", ids(CLEAN))


class TitleAndDescriptionTests(unittest.TestCase):
    def test_missing_title(self):
        self.assertIn("title.missing", ids("<html><head></head><body><p>hi</p></body></html>"))

    def test_long_title(self):
        html = f"<html><head><title>{'palavra ' * 15}</title></head><body></body></html>"
        self.assertIn("title.too_long", ids(html))

    def test_duplicate_meta_description(self):
        html = ('<html><head><title>x</title>'
                '<meta name="description" content="one">'
                '<meta name="description" content="two"></head><body></body></html>')
        self.assertIn("description.multiple", ids(html))


class IndexabilityTests(unittest.TestCase):
    def test_meta_noindex(self):
        html = '<html><head><meta name="robots" content="noindex, follow"></head><body></body></html>'
        self.assertIn("indexability.noindex", ids(html))

    def test_x_robots_tag_header(self):
        found = ids("<html><head><title>x</title></head><body></body></html>",
                    headers={"x-robots-tag": "googlebot: noindex"})
        self.assertIn("indexability.noindex", found)

    def test_max_image_preview_is_not_a_block(self):
        html = ('<html><head><meta name="robots" content="max-image-preview:large">'
                "</head><body></body></html>")
        self.assertNotIn("indexability.noindex", ids(html))


class CanonicalTests(unittest.TestCase):
    def test_cross_domain_canonical(self):
        html = '<html><head><link rel="canonical" href="https://other.test/x"></head><body></body></html>'
        self.assertIn("canonical.cross_domain", ids(html))

    def test_conflicting_canonicals(self):
        html = ('<html><head><link rel="canonical" href="/a">'
                '<link rel="canonical" href="/b"></head><body></body></html>')
        self.assertIn("canonical.multiple", ids(html))

    def test_relative_canonical(self):
        html = '<html><head><link rel="canonical" href="/loja/sapatos"></head><body></body></html>'
        self.assertIn("canonical.relative", ids(html))


class HeadingTests(unittest.TestCase):
    def test_missing_h1_and_skipped_level(self):
        found = ids("<html><body><h2>a</h2><h4>b</h4></body></html>")
        self.assertIn("headings.no_h1", found)
        self.assertIn("headings.skipped_level", found)

    def test_empty_heading(self):
        self.assertIn("headings.empty", ids("<html><body><h1>a</h1><h2></h2></body></html>"))


class ImageTests(unittest.TestCase):
    def test_missing_alt(self):
        self.assertIn("images.missing_alt", ids('<html><body><img src="a.png"></body></html>'))

    def test_empty_alt_is_not_missing(self):
        self.assertNotIn("images.missing_alt",
                         ids('<html><body><img src="a.png" alt=""></body></html>'))

    def test_filename_alt(self):
        found = ids('<html><body><img src="a.png" alt="IMG_2231.jpg"></body></html>')
        self.assertIn("images.alt_is_filename", found)

    def test_missing_dimensions(self):
        found = ids('<html><body><img src="a.png" alt="um sapato"></body></html>')
        self.assertIn("images.missing_dimensions", found)


class LinkTests(unittest.TestCase):
    def test_generic_anchor_in_portuguese(self):
        html = '<html><body><a href="/x">saiba mais</a></body></html>'
        self.assertIn("links.generic_anchor", ids(html))

    def test_image_link_is_not_an_empty_anchor(self):
        html = '<html><body><a href="/x"><img src="i.png" alt="ver produto"></a></body></html>'
        self.assertNotIn("links.empty_anchor", ids(html))

    def test_insecure_link_from_https_page(self):
        html = '<html><body><a href="http://ex.test/x">a pagina antiga</a></body></html>'
        self.assertIn("links.insecure", ids(html))


class SocialAndStructuredTests(unittest.TestCase):
    def test_relative_og_image(self):
        html = ('<html><head><meta property="og:title" content="t">'
                '<meta property="og:image" content="/og.png"></head><body></body></html>')
        self.assertIn("social.og_image_relative", ids(html))

    def test_invalid_json_ld(self):
        html = ('<html><head><script type="application/ld+json">{"@type":"Product",}'
                "</script></head><body></body></html>")
        self.assertIn("structured.invalid_json", ids(html))

    def test_missing_required_properties(self):
        html = ('<html><head><script type="application/ld+json">'
                '{"@type":"Product","name":"Sapato"}</script></head><body></body></html>')
        self.assertIn("structured.missing_properties", ids(html))


class I18nTests(unittest.TestCase):
    def test_missing_lang(self):
        self.assertIn("i18n.missing_lang", ids("<html><body>a</body></html>"))

    def test_hreflang_without_self_reference(self):
        html = ('<html lang="pt"><head>'
                '<link rel="alternate" hreflang="en" href="https://ex.test/en/">'
                "</head><body></body></html>")
        found = ids(html)
        self.assertIn("i18n.hreflang_no_self", found)
        self.assertIn("i18n.hreflang_no_xdefault", found)

    def test_invalid_hreflang_code(self):
        """Underscores and spelled-out languages are the errors people actually make."""
        for bad in ("pt_PT", "portugues", "pt-Portugal"):
            html = (f'<html lang="pt"><head>'
                    f'<link rel="alternate" hreflang="{bad}" href="/pt/">'
                    "</head><body></body></html>")
            with self.subTest(hreflang=bad):
                self.assertIn("i18n.hreflang_invalid", ids(html))

    def test_valid_hreflang_codes_are_accepted(self):
        for good in ("pt", "pt-PT", "PT-pt", "zh-Hant-TW", "es-419", "x-default"):
            html = (f'<html lang="pt"><head>'
                    f'<link rel="alternate" hreflang="{good}" href="https://ex.test/loja/sapatos">'
                    "</head><body></body></html>")
            with self.subTest(hreflang=good):
                self.assertNotIn("i18n.hreflang_invalid", ids(html))


class TechnicalTests(unittest.TestCase):
    def test_mixed_content(self):
        html = '<html><body><script src="http://cdn.test/a.js"></script></body></html>'
        self.assertIn("technical.mixed_content", ids(html))

    def test_http_page(self):
        self.assertIn("technical.not_https",
                      ids("<html><body>a</body></html>", url="http://ex.test/x"))

    def test_missing_viewport(self):
        self.assertIn("technical.no_viewport", ids("<html><head></head><body>a</body></html>"))

    def test_client_error(self):
        self.assertIn("technical.client_error",
                      ids("<html><body>not found</body></html>", status=404))

    def test_slow_server(self):
        self.assertIn("technical.ttfb_very_slow", ids(CLEAN, ttfb=2500.0))

    def test_uncompressed_html(self):
        found = ids(CLEAN, headers={"content-encoding": ""})
        self.assertIn("technical.no_compression", found)


class BlockedPageTests(unittest.TestCase):
    """A challenge page must not be described as if it were the site."""

    CHALLENGE = "<html><head><title></title></head><body><script>waf()</script></body></html>"

    def test_blocked_page_reports_only_the_block(self):
        findings = analyze(self.CHALLENGE, status=202, error=ErrorKind.BLOCKED)
        self.assertEqual([f.id for f in findings], ["technical.blocked"])

    def test_failed_fetch_reports_only_the_failure(self):
        findings = analyze(None, status=None, error=ErrorKind.TIMEOUT)
        self.assertEqual([f.id for f in findings], ["technical.fetch_failed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
