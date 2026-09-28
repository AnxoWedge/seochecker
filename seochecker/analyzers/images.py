"""Image alternatives, and layout stability.

Alt text is the one thing Google singles out: "the most important attribute when
it comes to providing metadata for an image", and the same string is what a
screen reader announces. So this checks more than whether the attribute exists —
whether it says anything, and whether the *other* elements that owe the reader an
alternative have one.

Two refinements that keep it honest rather than loud. An image named through
`aria-label` is not inaccessible, so it is reported as the lesser problem it is:
search engines read `alt`, assistive technology reads the accessible name, and
only one of those is satisfied. And a 1x1 tracking beacon is not content with a
missing description; it wants `alt=""`, which is a different sentence.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import PurePosixPath
from typing import Iterator
from urllib.parse import parse_qsl, unquote, urlsplit

from ..models import Finding
from .base import PageContext, analyzer, notice, sample, warning

# alt text that is really just the file it came off the camera as
# Alt text that is literally a filename. It must carry a digit run or an
# extension — a bare word like "image" is a *generic* alt, which is a different
# finding, and matching both reported one problem twice.
_FILENAME_ALT = re.compile(
    r"^\s*(img|dsc|dscn|pxl|screenshot|photo|image|foto|imagem|untitled)"
    r"(?:[-_ ]?\d+(?:\.\w{2,4})?|\.\w{2,4})\s*$",
    re.I,
)
_ANY_FILENAME = re.compile(r"^\s*\S+\.(jpe?g|png|gif|webp|avif|svg|bmp)\s*$", re.I)

# Google, on writing alt text: "Don't include extra words like 'Image of' or
# 'Photo of.'" The screen reader already says it is an image.
_REDUNDANT_PREFIX = re.compile(
    r"^\s*(an?\s+)?(image|photo|picture|graphic|icon|logo|screenshot|illustration)"
    r"\s+(of|showing|depicting)\b", re.I)

# Alt text that occupies the attribute without describing anything.
_GENERIC_ALT = frozenset({
    "image", "images", "photo", "photos", "picture", "pictures", "graphic",
    "icon", "logo", "banner", "thumbnail", "untitled", "spacer", "blank",
    "imagem", "imagens", "foto", "fotos", "logotipo", "grafico", "icone",
})

# Google, on filenames: "Avoid using generic filenames like 'image1.jpg',
# 'pic.gif', or '1.jpg' when possible."
_GENERIC_FILENAME = re.compile(
    r"^(?:(?:image|img|photo|pic|picture|untitled|final|new|download|foto|imagem)"
    r"[-_ ]?\d*|(?:dsc|dscn|pxl|screenshot|photo|img)[-_ ]?\d+|\d+)$", re.I)

# Image proxies put the real file in a query parameter, so the path is always
# something like /_next/image or /cdn-cgi/image and always reads as generic.
_PROXY_PATHS = ("/_next/image", "/cdn-cgi/image", "/imgproxy", "/_vercel/image",
                "/_ipx", "/wp-content/uploads/", "/i/", "/cdn/")
_PROXY_PARAMS = ("url", "src", "image", "uri")

# Below this, repeating one alt string is normal (a repeated logo, say).
DUPLICATE_ALT_THRESHOLD = 4


@analyzer
def image_alts(ctx: PageContext) -> Iterator[Finding]:
    if not ctx.doc:
        return
    images = ctx.doc.images
    if not images:
        return

    # Genuinely undescribed: no alt, no accessible name, not marked decorative,
    # and not a tracking beacon.
    missing = [img for img in images
               if not img.has_alt and not img.named_without_alt
               and not img.decorative and not img.tracking_pixel]
    if missing:
        yield warning(
            "images.missing_alt",
            f"{len(missing)} of {len(images)} images have no alt attribute",
            evidence=sample([img.src or "(no src)" for img in missing]),
            fix="Describe what the image shows. Use alt=\"\" — present but empty — for purely "
                "decorative images, so assistive technology knows to skip them.",
        )

    named_elsewhere = [img for img in images if img.named_without_alt]
    if named_elsewhere:
        yield notice(
            "images.alt_via_aria",
            f"{len(named_elsewhere)} image(s) are named by aria-label or title, not alt",
            evidence=sample([f"{img.src}: {img.accessible_name[:50]}"
                             for img in named_elsewhere], 3),
            fix="A screen reader will announce these, so they are not inaccessible. But "
                "Google reads the alt attribute, so add alt with the same text.",
        )

    beacons = [img for img in images if img.tracking_pixel and not img.has_alt]
    if beacons:
        yield notice(
            "images.beacon_without_empty_alt",
            f"{len(beacons)} tracking pixel(s) have no alt attribute",
            evidence=sample([img.src for img in beacons], 3),
            fix="Give them alt=\"\" so assistive technology skips them silently, rather than "
                "reading out a filename.",
        )

    redundant = [img for img in images if img.alt and _REDUNDANT_PREFIX.match(img.alt)]
    if redundant:
        yield notice(
            "images.redundant_alt_prefix",
            f"{len(redundant)} alt text(s) begin with \"image of\" or similar",
            evidence=sample([f"{img.src}: {img.alt[:50]}" for img in redundant], 3),
            fix="Google asks you not to: \"Don't include extra words like 'Image of' or "
                "'Photo of.'\" A screen reader already announces that it is an image.",
        )

    generic = [img for img in images
               if img.alt and img.alt.strip().lower().strip(".:-") in _GENERIC_ALT]
    if generic:
        yield notice(
            "images.generic_alt",
            f"{len(generic)} alt text(s) say nothing specific",
            evidence=sample([f"{img.src}: {img.alt}" for img in generic], 3),
            fix="\"image\" or \"logo\" fills the attribute without describing anything. Say "
                "what it shows, or use alt=\"\" if it shows nothing that matters.",
        )

    repeated = Counter(img.alt.strip().lower() for img in images
                       if img.alt and img.alt.strip())
    overused = [(text, n) for text, n in repeated.items() if n >= DUPLICATE_ALT_THRESHOLD]
    if overused:
        yield notice(
            "images.duplicate_alt",
            f"{len(overused)} alt text(s) are repeated across many images",
            evidence=sample([f"{n}x {text[:50]!r}" for text, n in overused], 3),
            fix="Identical alt text on different images tells a reader they are the same "
                "picture. Describe each one, or mark the repeated decoration alt=\"\".",
        )

    long_alts = [img for img in images
                 if img.alt and len(img.alt) > ctx.thresholds.alt_max]
    if long_alts:
        yield notice(
            "images.alt_too_long",
            f"{len(long_alts)} alt text(s) over {ctx.thresholds.alt_max} characters",
            evidence=sample([f"{img.src}: {img.alt[:60]}…" for img in long_alts], 3),
            fix="Keep alt text to a sentence. Longer descriptions belong in a caption or "
                "adjacent text.",
        )

    filename_alts = [img for img in images if img.alt
                     and (_FILENAME_ALT.match(img.alt) or _ANY_FILENAME.match(img.alt))]
    if filename_alts:
        yield notice(
            "images.alt_is_filename",
            f"{len(filename_alts)} alt text(s) are just a filename",
            evidence=sample([f"{img.src}: {img.alt}" for img in filename_alts], 3),
            fix="Replace with a description of the image's content.",
        )

    duplicate_of_src = [
        img for img in images
        if img.alt and img.url
        and img.alt.strip().lower() == PurePosixPath(urlsplit(img.url).path).stem.lower()
    ]
    if duplicate_of_src:
        yield notice(
            "images.alt_matches_slug",
            f"{len(duplicate_of_src)} alt text(s) just repeat the file's slug",
            evidence=sample([f"{img.src}: {img.alt}" for img in duplicate_of_src], 3),
            fix="Write for a person, not for the file system.",
        )


@analyzer
def other_alt_targets(ctx: PageContext) -> Iterator[Finding]:
    """Things that need an alternative and are not `<img>`.

    The HTML spec requires alt on `<input type="image">` and `<area>`; ARIA
    requires a name on `role="img"`. An inline `<svg>` that is the whole content
    of a link or button leaves that control with nothing to announce at all.
    """
    if not ctx.doc:
        return

    unnamed = [target for target in ctx.doc.alt_targets if target.unnamed]
    if not unnamed:
        return

    by_kind = Counter(target.kind for target in unnamed)
    yield warning(
        "images.control_without_name",
        f"{len(unnamed)} element(s) that need an alternative have none: "
        + ", ".join(f"{n} {kind}" for kind, n in by_kind.most_common()),
        evidence=sample([f"{t.kind}: {t.identifier}" for t in unnamed], 4),
        fix="An image button, image-map area or icon-only link with no accessible name is "
            "announced as nothing at all. Add alt on <input type=\"image\"> and <area>, and "
            "either a <title> inside the SVG or aria-label on the link.",
    )


@analyzer
def image_filenames(ctx: PageContext) -> Iterator[Finding]:
    """Google: avoid generic filenames like image1.jpg, pic.gif or 1.jpg."""
    if not ctx.doc:
        return

    generic = []
    for image in ctx.doc.images:
        if not image.url or image.tracking_pixel:
            continue
        parts = urlsplit(image.url)
        path = parts.path

        # Follow an image proxy to the file it is actually serving, or skip it:
        # judging /_next/image as a generic filename is judging the framework.
        if any(marker in path for marker in _PROXY_PATHS[:6]):
            query = dict(parse_qsl(parts.query))
            target = next((query[key] for key in _PROXY_PARAMS if query.get(key)), "")
            if not target:
                continue
            path = urlsplit(unquote(target)).path

        stem = PurePosixPath(path).stem
        if stem and _GENERIC_FILENAME.match(stem):
            generic.append(image.src)
    if generic:
        yield notice(
            "images.generic_filename",
            f"{len(generic)} image(s) have a generic filename",
            evidence=sample(generic, 4),
            fix="Google asks for filenames that are \"short but descriptive\", and to avoid "
                "generic ones like image1.jpg or 1.jpg. The filename is a ranking signal for "
                "image search in its own right.",
        )


@analyzer
def image_delivery(ctx: PageContext) -> Iterator[Finding]:
    if not ctx.doc:
        return
    images = ctx.doc.images
    if not images:
        return

    if broken := [img for img in images if not img.src]:
        yield warning(
            "images.missing_src",
            f"{len(broken)} <img> tag(s) with no src",
            fix="Remove them, or supply the source. If the src is set by JavaScript, "
                "crawlers will not see the image.",
        )

    sized = [img for img in images if img.src]
    unsized = [img for img in sized if not (img.width and img.height)]
    if unsized and sized:
        yield notice(
            "images.missing_dimensions",
            f"{len(unsized)} of {len(sized)} images have no width/height",
            evidence=sample([img.src for img in unsized]),
            fix="Set width and height (or aspect-ratio in CSS) so the browser reserves space. "
                "This is the usual cause of a poor Cumulative Layout Shift score.",
        )

    if len(sized) >= ctx.thresholds.images_lazy_threshold:
        eager = [img for img in sized if img.loading != "lazy"]
        if len(eager) == len(sized):
            yield notice(
                "images.no_lazy_loading",
                f"None of the {len(sized)} images use loading=\"lazy\"",
                fix="Add loading=\"lazy\" to images below the fold. Leave the hero image eager "
                    "so it does not delay Largest Contentful Paint.",
            )
