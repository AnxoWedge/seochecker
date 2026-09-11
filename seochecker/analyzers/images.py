"""Image accessibility and layout stability."""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Iterator
from urllib.parse import urlsplit

from ..models import Finding
from .base import PageContext, analyzer, notice, sample, warning

# alt text that is really just the file it came off the camera as
_FILENAME_ALT = re.compile(
    r"^\s*(img|dsc|dscn|pxl|screenshot|photo|image|foto|imagem|untitled)[-_ ]?\d*"
    r"(\.\w{2,4})?\s*$",
    re.I,
)
_ANY_FILENAME = re.compile(r"^\s*\S+\.(jpe?g|png|gif|webp|avif|svg|bmp)\s*$", re.I)


@analyzer
def image_alts(ctx: PageContext) -> Iterator[Finding]:
    if not ctx.doc:
        return
    images = ctx.doc.images
    if not images:
        return

    missing = [img for img in images if not img.has_alt]
    if missing:
        yield warning(
            "images.missing_alt",
            f"{len(missing)} of {len(images)} images have no alt attribute",
            evidence=sample([img.src or "(no src)" for img in missing]),
            fix="Describe what the image shows. Use alt=\"\" — present but empty — for purely "
                "decorative images, so assistive technology knows to skip them.",
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
