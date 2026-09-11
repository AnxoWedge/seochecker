"""Phase 7 acceptance tests: scoring and the report writers.

The scoring tests are the calibration table itself. They exist because the first
model scored a site with every page set to noindex — invisible to search — at
94.8 out of 100, and nothing but a stated expectation catches that.
"""

from __future__ import annotations

import csv
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seochecker.models import Finding, Page, Severity
from seochecker.report import RunStore, diff_runs, write_csv, write_html
from seochecker.report.html_out import build_context, group_findings, render_html
from seochecker.score import evaluated_categories, grade_for, score

CATEGORIES = evaluated_categories(crawled=True, rendered=False, external=False)
URLS = [f"https://e.test/{i}" for i in range(100)]


def findings(finding_id: str, severity: Severity, urls, affected: int = 0) -> list[Finding]:
    return [Finding(id=finding_id, severity=severity, category=finding_id.split(".")[0],
                    message="msg", url=url, affected=affected) for url in urls]


def overall(items: list[Finding], pages: int = 100) -> float:
    return score(items, pages=pages, categories=CATEGORIES).overall


class ScoringTests(unittest.TestCase):
    """Each case states what the number should mean before it states the number."""

    def test_a_clean_site_scores_full_marks(self):
        self.assertEqual(overall([]), 100.0)

    def test_a_site_invisible_to_search_scores_zero(self):
        self.assertEqual(overall(findings("indexability.noindex", Severity.CRITICAL, URLS)), 0.0)

    def test_one_critical_on_one_page_in_a_hundred_barely_registers(self):
        self.assertGreater(overall(findings("title.missing", Severity.CRITICAL, URLS[:1])), 95)

    def test_the_same_critical_everywhere_is_catastrophic(self):
        self.assertLess(overall(findings("title.missing", Severity.CRITICAL, URLS)), 40)

    def test_breadth_matters(self):
        few = overall(findings("title.missing", Severity.CRITICAL, URLS[:5]))
        many = overall(findings("title.missing", Severity.CRITICAL, URLS[:50]))
        self.assertGreater(few, many)

    def test_severity_matters(self):
        critical = overall(findings("title.missing", Severity.CRITICAL, URLS))
        warning = overall(findings("title.too_long", Severity.WARNING, URLS))
        notice = overall(findings("title.too_short", Severity.NOTICE, URLS))
        self.assertLess(critical, warning)
        self.assertLess(warning, notice)

    def test_info_findings_never_deduct(self):
        self.assertEqual(overall(findings("structured.present", Severity.INFO, URLS)), 100.0)

    def test_important_categories_cost_more(self):
        indexability = overall(findings("indexability.noindex", Severity.WARNING, URLS))
        social = overall(findings("social.og_missing", Severity.WARNING, URLS))
        self.assertLess(indexability, social)

    def test_site_level_finding_is_scored_on_the_pages_it_names(self):
        """A near-duplicate pair on a 12-page site is not a site-wide problem."""
        narrow = findings("duplicate.near_content", Severity.WARNING, [None], affected=2)
        broad = findings("duplicate.near_content", Severity.WARNING, [None])
        self.assertGreater(overall(narrow, pages=12), overall(broad, pages=12))

    def test_unevaluated_categories_are_not_scored_as_clean(self):
        """Scoring a check that never ran as a perfect 100 would inflate the result."""
        without = evaluated_categories(crawled=True, rendered=False, external=False)
        with_external = evaluated_categories(crawled=True, rendered=False, external=True)
        self.assertNotIn("external", without)
        self.assertIn("external", with_external)

    def test_category_scores_locate_the_problem(self):
        card = score(findings("images.missing_alt", Severity.WARNING, URLS),
                     pages=100, categories=CATEGORIES)
        worst = card.categories[0]
        self.assertEqual(worst.category, "images")
        self.assertLess(worst.score, 100)
        self.assertEqual(worst.deductions[0].finding_id, "images.missing_alt")

    def test_deductions_are_auditable(self):
        card = score(findings("title.missing", Severity.CRITICAL, URLS[:10]),
                     pages=100, categories=CATEGORIES)
        deduction = next(c for c in card.categories if c.category == "title").deductions[0]
        self.assertEqual(deduction.affected_pages, 10)
        self.assertAlmostEqual(deduction.share, 0.1)
        self.assertGreater(deduction.overall_points, 0)

    def test_grades(self):
        self.assertEqual(grade_for(95), "A")
        self.assertEqual(grade_for(85), "B")
        self.assertEqual(grade_for(0), "F")


def sample_pages() -> list[Page]:
    page = Page(requested_url="https://e.test/", final_url="https://e.test/", status=200,
                mime="text/html", headers={})
    page.seo = {"word_count": 400}
    page.pagerank, page.click_depth, page.inlink_count = 0.5, 0, 3
    page.findings = findings("title.missing", Severity.CRITICAL, ["https://e.test/"])
    other = Page(requested_url="https://e.test/a", final_url="https://e.test/a", status=404,
                 mime="text/html", headers={})
    other.seo = {"word_count": 10}
    return [page, other]


class HtmlReportTests(unittest.TestCase):
    def setUp(self):
        self.pages = sample_pages()
        self.context = build_context(
            target="https://e.test/",
            pages=self.pages,
            site_findings=findings("sitemap.missing", Severity.WARNING, [None]),
            score=score([f for p in self.pages for f in p.findings], pages=2,
                        categories=CATEGORIES),
            graph={"nodes": 2, "edges": 1, "orphans": 0, "unreachable": 0, "dead_ends": 1,
                   "max_click_depth": 1, "authority_spread": 1.0, "top_by_pagerank": []},
        )
        self.html = render_html(self.context)

    def test_it_is_self_contained(self):
        """It has to open from an email attachment on a laptop with no internet."""
        external = re.findall(r'(?:src|href)="(https?://[^"]+)"', self.html)
        offsite = [u for u in external if "e.test" not in u]
        self.assertEqual(offsite, [], f"report loads external resources: {offsite}")

    def test_it_contains_the_substance(self):
        for fragment in ("SEO report", "https://e.test/", "title.missing",
                         "sitemap.missing", "Grade"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, self.html)

    def test_content_is_escaped(self):
        page = sample_pages()[0]
        page.findings = [Finding(id="x.y", severity=Severity.WARNING, category="x",
                                 message="<script>alert(1)</script>", url="https://e.test/")]
        html = render_html(build_context(target="https://e.test/", pages=[page],
                                         site_findings=[]))
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_findings_are_grouped_by_id(self):
        pages = sample_pages()
        pages[1].findings = findings("title.missing", Severity.CRITICAL, ["https://e.test/a"])
        grouped = group_findings(pages)
        self.assertEqual(len(grouped), 1)
        self.assertEqual(len(grouped[0]["urls"]), 2)

    def test_it_writes_to_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_html(Path(tmp) / "out" / "report.html", self.context)
            self.assertTrue(path.exists())
            self.assertGreater(path.stat().st_size, 2000)


class CsvTests(unittest.TestCase):
    def test_one_row_per_finding(self):
        items = (findings("title.missing", Severity.CRITICAL, URLS[:3])
                 + findings("images.missing_alt", Severity.WARNING, URLS[:2]))
        with tempfile.TemporaryDirectory() as tmp:
            path = write_csv(Path(tmp) / "f.csv", items)
            with path.open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 5)
        self.assertEqual(rows[0]["severity"], "critical")
        self.assertEqual(rows[0]["finding_id"], "title.missing")
        self.assertEqual(rows[0]["url"], URLS[0])


class StoreTests(unittest.TestCase):
    def test_diff_between_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            with RunStore(Path(tmp) / "runs.sqlite3") as store:
                store.record(target="https://e.test/", pages=5, score=60.0, grade="D",
                             findings=findings("title.missing", Severity.CRITICAL, URLS[:1]))
                store.record(target="https://e.test/", pages=5, score=85.0, grade="B",
                             findings=findings("images.missing_alt", Severity.WARNING, URLS[:1]))
                diff = diff_runs(store, "https://e.test/")
        self.assertEqual(diff.score_change, 25.0)
        self.assertEqual([f[1] for f in diff.introduced], ["images.missing_alt"])
        self.assertEqual([f[1] for f in diff.resolved], ["title.missing"])

    def test_first_run_has_nothing_to_compare(self):
        with tempfile.TemporaryDirectory() as tmp:
            with RunStore(Path(tmp) / "runs.sqlite3") as store:
                store.record(target="https://e.test/", pages=1, findings=[])
                self.assertIsNone(diff_runs(store, "https://e.test/"))

    def test_targets_are_kept_apart(self):
        with tempfile.TemporaryDirectory() as tmp:
            with RunStore(Path(tmp) / "runs.sqlite3") as store:
                store.record(target="https://a.test/", pages=1, findings=[])
                store.record(target="https://b.test/", pages=1, findings=[])
                self.assertEqual(len(store.runs_for("https://a.test/")), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
