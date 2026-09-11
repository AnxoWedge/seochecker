"""Phase 3 acceptance tests.

`test_bundled_rules_are_valid` is the guard that matters day to day: rules.yaml is
data, edited far more often than the engine, and a typo there should fail here
rather than silently stop detecting something.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seochecker.fingerprint import Fingerprinter, RulesError, load_rules
from seochecker.fingerprint.detect import _combine, longest_literal
from seochecker.html import Document
from seochecker.models import Page

URL = "https://ex.test/"


def fingerprint(html: str | None = None, *, headers: dict[str, str] | None = None,
                cookies: dict[str, str] | None = None, url: str = URL,
                min_confidence: float = 0.5) -> dict[str, object]:
    page = Page(
        requested_url=url,
        final_url=url,
        status=200,
        mime="text/html" if html is not None else "application/pdf",
        headers={k.lower(): v for k, v in (headers or {}).items()},
        cookies=cookies or {},
        html=html,
    )
    doc = Document(html, url) if html is not None else None
    detections = Fingerprinter(min_confidence=min_confidence).detect(page, doc)
    return {d.name: d for d in detections}


class RulesFileTests(unittest.TestCase):
    def test_bundled_rules_are_valid(self):
        technologies, categories = load_rules()
        self.assertGreater(len(technologies), 50)
        for name, tech in technologies.items():
            with self.subTest(technology=name):
                self.assertIn(tech.category, categories)
                self.assertTrue(tech.signals, f"{name} has no signals")
                for signal in tech.signals:
                    self.assertGreaterEqual(signal.confidence, 0.0)
                    self.assertLessEqual(signal.confidence, 1.0)

    def test_unknown_category_is_rejected(self):
        self._expect_error("""
categories: {cms: CMS}
technologies:
  Thing:
    category: nonsense
    signals: [{type: html, pattern: 'x'}]
""")

    def test_unknown_signal_type_is_rejected(self):
        self._expect_error("""
categories: {cms: CMS}
technologies:
  Thing:
    category: cms
    signals: [{type: telepathy, pattern: 'x'}]
""")

    def test_dangling_implication_is_rejected(self):
        self._expect_error("""
categories: {cms: CMS}
technologies:
  Thing:
    category: cms
    implies: [Ghosts]
    signals: [{type: html, pattern: 'x'}]
""")

    def test_bad_regex_is_rejected(self):
        self._expect_error("""
categories: {cms: CMS}
technologies:
  Thing:
    category: cms
    signals: [{type: html, pattern: '([unclosed'}]
""")

    def _expect_error(self, yaml_text: str) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as handle:
            handle.write(yaml_text)
            path = Path(handle.name)
        try:
            with self.assertRaises(RulesError):
                load_rules(path)
        finally:
            path.unlink()


class LiteralPrefilterTests(unittest.TestCase):
    """The pre-filter must never cost a detection. Erring towards '' is free."""

    def test_extracts_required_literals(self):
        cases = {
            r"/wp-content/": "/wp-content/",
            r"cdn\.shopify\.com": "cdn.shopify.com",
            r"elementor-(?:widget|section|element)": "elementor-",
            r"^WordPress(?:\s+(\d[\w.\-]*))?": "wordpress",
            r"/static/version\d+/": "/static/version",
            r'class="[^"]*woocommerce': "woocommerce",
            r"GTM-[A-Z0-9]{4,}": "gtm-",
        }
        for pattern, expected in cases.items():
            with self.subTest(pattern=pattern):
                self.assertEqual(longest_literal(pattern), expected)

    def test_optional_and_alternating_parts_are_not_treated_as_required(self):
        # A top-level alternation guarantees nothing...
        self.assertEqual(longest_literal(r"__VUE__|__vue_app__"), "")
        self.assertEqual(longest_literal(r"_nghost-|_ngcontent-"), "")
        # ...and neither does anything a quantifier can reduce to nothing.
        self.assertNotIn("z", longest_literal(r"abcdefz?"))
        self.assertNotIn("z", longest_literal(r"abcdefz*"))
        self.assertNotIn("z", longest_literal(r"abcdefz{0,2}"))

    def test_every_literal_comes_from_its_own_pattern(self):
        """A literal the engine invented would silently suppress its rule forever."""
        technologies, _ = load_rules()
        checked = 0
        for tech in technologies.values():
            for signal in tech.signals:
                if signal.type != "html" or not signal.literal:
                    continue
                checked += 1
                source = signal.pattern.pattern.replace("\\", "").lower()
                with self.subTest(technology=tech.name):
                    self.assertIn(signal.literal, source,
                                  f"{tech.name}: pre-filter literal is not in its own pattern")
        self.assertGreater(checked, 10, "expected the rules to exercise this")

    def test_prefilter_does_not_change_detections(self):
        html = ('<html><head><meta name="generator" content="WooCommerce 9.4">'
                '</head><body>/wp-content/ /wp-includes/ cdn.shopify.com'
                ' elementor-widget GTM-ABCD1234 drupal-settings-json'
                ' static.hotjar.com framerusercontent.com __sveltekit'
                ' Static.SQUARESPACE_CONTEXT /_next/static/ __NUXT__</body></html>')
        with_filter = fingerprint(html)

        engine = Fingerprinter()
        for tech in engine.technologies.values():
            for signal in tech.signals:
                signal.literal = ""
        page = Page(requested_url=URL, final_url=URL, status=200, mime="text/html", html=html)
        without = {d.name: d for d in engine.detect(page, Document(html, URL))}

        self.assertEqual(
            {n: d.confidence for n, d in with_filter.items()},
            {n: d.confidence for n, d in without.items()},
        )


class ConfidenceTests(unittest.TestCase):
    def test_independent_signals_accumulate(self):
        self.assertAlmostEqual(_combine([0.7]), 0.7)
        self.assertAlmostEqual(_combine([0.7, 0.7]), 0.91)
        self.assertLess(_combine([0.7, 0.7, 0.7]), 1.0)

    def test_more_evidence_scores_higher(self):
        one = fingerprint('<html><head></head><body>/wp-content/x.png</body></html>')
        two = fingerprint('<html><head></head><body>/wp-content/ /wp-includes/</body></html>')
        self.assertLess(one["WordPress"].confidence, two["WordPress"].confidence)


class DetectionTests(unittest.TestCase):
    def test_wordpress_generator_with_version(self):
        found = fingerprint(
            '<html><head><meta name="generator" content="WordPress 6.7.1">'
            "</head><body></body></html>"
        )
        self.assertIn("WordPress", found)
        self.assertEqual(found["WordPress"].version, "6.7.1")
        self.assertEqual(found["WordPress"].confidence, 1.0)

    def test_prerelease_version_is_kept_whole(self):
        found = fingerprint(
            '<html><head><meta name="generator" content="WordPress 7.2-alpha-63586">'
            "</head><body></body></html>"
        )
        self.assertEqual(found["WordPress"].version, "7.2-alpha-63586")

    def test_implications_are_transitive(self):
        found = fingerprint(
            '<html><head><meta name="generator" content="WooCommerce 9.4">'
            "</head><body></body></html>"
        )
        self.assertIn("WooCommerce", found)
        self.assertEqual(found["WordPress"].implied_by, "WooCommerce")
        self.assertEqual(found["PHP"].implied_by, "WordPress")
        # Confidence decays down the chain rather than being inherited whole.
        self.assertLess(found["PHP"].confidence, found["WordPress"].confidence)
        self.assertLess(found["WordPress"].confidence, found["WooCommerce"].confidence)

    def test_direct_detection_beats_implication(self):
        found = fingerprint(
            '<html><head><meta name="generator" content="WooCommerce 9.4">'
            "</head><body>/wp-content/ /wp-includes/</body></html>",
            headers={"x-pingback": "https://ex.test/xmlrpc.php"},
        )
        self.assertEqual(found["WordPress"].implied_by, "")
        self.assertGreater(found["WordPress"].confidence, 0.99)

    def test_implication_can_raise_a_weak_direct_detection(self):
        """A weak WordPress signal, next to a certain WooCommerce, is not weak."""
        rest_header = {"link": "<https://ex.test/wp-json/>; rel=\"https://api.w.org/\""}
        weak = fingerprint("<html><body></body></html>", headers=rest_header)
        strong = fingerprint(
            '<html><head><meta name="generator" content="WooCommerce 9.4">'
            "</head><body></body></html>",
            headers=rest_header,
        )
        self.assertEqual(weak["WordPress"].confidence, 0.8)
        self.assertEqual(strong["WordPress"].implied_by, "")   # direct evidence kept
        self.assertGreater(strong["WordPress"].confidence, weak["WordPress"].confidence)

    def test_header_signal(self):
        found = fingerprint("<html></html>", headers={"x-shopify-stage": "production"})
        self.assertIn("Shopify", found)

    def test_cookie_signal(self):
        found = fingerprint("<html></html>", cookies={"PHPSESSID": "abc123"})
        self.assertIn("PHP", found)

    def test_dom_attribute_version(self):
        found = fingerprint('<html><body><app-root ng-version="17.3.1"></app-root></body></html>')
        self.assertIn("Angular", found)
        self.assertEqual(found["Angular"].version, "17.3.1")

    def test_server_header_version(self):
        found = fingerprint("<html></html>", headers={"server": "nginx/1.24.0"})
        self.assertEqual(found["nginx"].version, "1.24.0")

    def test_headers_still_work_without_html(self):
        found = fingerprint(None, headers={"cf-ray": "abc-LIS", "server": "cloudflare"})
        self.assertIn("Cloudflare", found)

    def test_plain_page_detects_nothing(self):
        found = fingerprint("<html><head><title>hi</title></head><body><p>a</p></body></html>")
        self.assertEqual(found, {})

    def test_min_confidence_filters(self):
        html = '<html><body><script src="/js/react-dom.production.min.js"></script></body></html>'
        self.assertIn("React", fingerprint(html, min_confidence=0.5))
        self.assertNotIn("React", fingerprint(html, min_confidence=0.8))

    def test_evidence_is_recorded(self):
        found = fingerprint("<html></html>", headers={"x-vercel-id": "cdg1::abc"})
        self.assertTrue(found["Vercel"].evidence)
        self.assertIn("x-vercel-id", found["Vercel"].evidence[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
