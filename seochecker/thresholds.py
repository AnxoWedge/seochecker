"""Every tunable number in one place.

Search advice ages badly, so these are values you argue with, not constants
buried in an `if`. The CLI can override any of them later without a code change.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class Thresholds:
    # Titles and descriptions are really limited by pixel width, not characters;
    # these character counts are the usual practical proxy for the SERP cutoff.
    title_min: int = 30
    title_max: int = 60
    description_min: int = 70
    description_max: int = 160

    alt_max: int = 125            # screen readers get unwieldy past this
    images_lazy_threshold: int = 5  # only nag about lazy-loading on image-heavy pages

    thin_content: int = 300
    very_thin_content: int = 100
    text_ratio_min: float = 0.10

    ttfb_slow_ms: float = 800.0   # Google's "good" server response time
    ttfb_very_slow_ms: float = 1800.0

    html_bytes_warn: int = 150_000
    max_links: int = 150
    render_blocking_warn: int = 3
    redirect_chain_warn: int = 2
