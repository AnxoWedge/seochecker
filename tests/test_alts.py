"""Alt text detection, across every element that owes the reader an alternative.

Half of these tests are about what must *not* be reported. Alt checks run on
every image of every page, so a false positive here is multiplied by the size of
the site — both of the ones recorded below were found on live sites.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seochecker.analyzers import PageContext, run_page_analyzers
from seochecker.config import CrawlConfig
from seochecker.html import Document
from seochecker.models import Page

HEAD = ('<html lang="en"><head><meta charset="utf-8">'
        "<title>A page about handmade shoes from Guimaraes</title>"
        '<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
        "<body><h1>Shoes</h1><p>Body text so the page is not judged thin.</p>")


def findings(body: str) -> dict[str, str]:
    """Image findings for a fragment, as {id: evidence}."""
    html = HEAD + body + "</body></html>"
    page = Page(requested_url="https://e.test/p", final_url="https://e.test/p",
                status=200, mime="text/html",
                headers={"content-type": "text/html"}, html=html)
    page.timing.ttfb_ms = 100
    found = run_page_analyzers(PageContext(
        page=page, doc=Document(html, page.final_url),
        config=CrawlConfig(url=page.final_url)))
    return {f.id: f.evidence for f in found if f.id.startswith("images")}


class MissingAltTests(unittest.TestCase):
    def test_an_image_with_no_alt_is_reported(self):
        self.assertIn("images.missing_alt", findings('<img src="/a.jpg" width=9 height=9>'))

    def test_an_explicitly_decorative_image_is_not(self):
        for markup in ('<img src="/a.jpg" alt="" width=9 height=9>',
                       '<img src="/a.jpg" aria-hidden="true" width=9 height=9>',
                       '<img src="/a.jpg" role="presentation" width=9 height=9>'):
            with self.subTest(markup=markup):
                self.assertNotIn("images.missing_alt", findings(markup))

    def test_an_image_named_by_aria_is_a_lesser_finding_not_a_missing_alt(self):
        """It is announced correctly; it is only invisible to search engines."""
        found = findings('<img src="/a.jpg" aria-label="A leather sole" width=9 height=9>')
        self.assertNotIn("images.missing_alt", found)
        self.assertIn("images.alt_via_aria", found)

    def test_a_tracking_pixel_is_not_an_undescribed_image(self):
        found = findings('<img src="/beacon.gif" width="1" height="1">')
        self.assertNotIn("images.missing_alt", found)
        self.assertIn("images.beacon_without_empty_alt", found)


class AltQualityTests(unittest.TestCase):
    def test_redundant_prefixes(self):
        """Google: don't include extra words like 'Image of' or 'Photo of.'"""
        for alt in ("Photo of a leather sole", "Image of the workshop",
                    "A picture of the bench", "Icon showing a cart"):
            with self.subTest(alt=alt):
                self.assertIn("images.redundant_alt_prefix",
                              findings(f'<img src="/a.jpg" alt="{alt}" width=9 height=9>'))

    def test_a_real_description_is_left_alone(self):
        found = findings('<img src="/sole.jpg" alt="A cobbler stitching a leather sole" '
                         "width=9 height=9>")
        self.assertEqual(found.get("images.redundant_alt_prefix"), None)
        self.assertEqual(found.get("images.generic_alt"), None)

    def test_generic_alt_text(self):
        for alt in ("image", "Logo", "photo", "banner", "imagem"):
            with self.subTest(alt=alt):
                self.assertIn("images.generic_alt",
                              findings(f'<img src="/a.jpg" alt="{alt}" width=9 height=9>'))

    def test_generic_alt_and_filename_alt_are_not_both_reported(self):
        """They describe the same fault; reporting both counted it twice."""
        bare = findings('<img src="/a.jpg" alt="image" width=9 height=9>')
        self.assertIn("images.generic_alt", bare)
        self.assertNotIn("images.alt_is_filename", bare)

        real = findings('<img src="/a.jpg" alt="IMG_2231.jpg" width=9 height=9>')
        self.assertIn("images.alt_is_filename", real)
        self.assertNotIn("images.generic_alt", real)

    def test_the_same_alt_on_many_images(self):
        body = "".join(f'<img src="/{i}.jpg" alt="product" width=9 height=9>'
                       for i in range(5))
        self.assertIn("images.duplicate_alt", findings(body))

    def test_a_couple_of_repeats_is_normal(self):
        body = "".join(f'<img src="/{i}.jpg" alt="logo mark" width=9 height=9>'
                       for i in range(2))
        self.assertNotIn("images.duplicate_alt", findings(body))


class FilenameTests(unittest.TestCase):
    def test_generic_filenames(self):
        for src in ("/IMG_2231.jpg", "/image1.png", "/photo-4.jpg", "/1.jpg"):
            with self.subTest(src=src):
                self.assertIn("images.generic_filename",
                              findings(f'<img src="{src}" alt="A workshop bench" '
                                       "width=9 height=9>"))

    def test_a_descriptive_filename_is_left_alone(self):
        self.assertNotIn("images.generic_filename",
                         findings('<img src="/black-leather-oxford-shoe.jpg" '
                                  'alt="A black oxford" width=9 height=9>'))

    def test_an_image_proxy_is_followed_to_the_real_file(self):
        """/_next/image is the framework's path, not the author's filename.

        Judging it as generic flagged every image on every Next.js site.
        """
        descriptive = findings('<img src="/_next/image?url=%2Fblack-oxford-shoe.jpg&w=640" '
                               'alt="A black oxford" width=9 height=9>')
        self.assertNotIn("images.generic_filename", descriptive)

        generic = findings('<img src="/_next/image?url=%2Fphoto4.jpg&w=640" '
                           'alt="A black oxford" width=9 height=9>')
        self.assertIn("images.generic_filename", generic)


class OtherAltTargetTests(unittest.TestCase):
    """The HTML spec requires alt on more than `<img>`."""

    def test_an_image_button_without_alt(self):
        found = findings('<input type="image" src="/go.png">')
        self.assertIn("images.control_without_name", found)
        self.assertIn("input image", found["images.control_without_name"])

    def test_an_image_map_area_without_alt(self):
        self.assertIn("images.control_without_name",
                      findings('<map name="m"><area href="/a"></map>'))

    def test_an_icon_only_link_with_nothing_to_announce(self):
        self.assertIn("images.control_without_name",
                      findings('<a href="/download"><svg><rect/></svg></a>'))

    def test_a_named_control_is_not_reported(self):
        for markup in (
            '<input type="image" src="/go.png" alt="Search">',
            '<map name="m"><area href="/a" alt="Region A"></map>',
            '<a href="/d"><svg><title>Download the catalogue</title><rect/></svg></a>',
            # The name can live on the control itself, which is perfectly valid
            # and was briefly reported as a fault on wordpress.org.
            '<button aria-label="Open menu"><svg><rect/></svg></button>',
            '<a href="/d" aria-label="Download"><svg><rect/></svg></a>',
        ):
            with self.subTest(markup=markup):
                self.assertNotIn("images.control_without_name", findings(markup))

    def test_a_decorative_icon_beside_text_is_not_reported(self):
        self.assertNotIn("images.control_without_name",
                         findings('<a href="/d"><svg><rect/></svg> Download</a>'))

    def test_a_role_img_element_with_no_name(self):
        self.assertIn("images.control_without_name",
                      findings('<div role="img" class="chart"></div>'))


if __name__ == "__main__":
    unittest.main(verbosity=2)
