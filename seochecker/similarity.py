"""Near-duplicate detection by bottom-k MinHash over word shingles.

SimHash was the first attempt and had to be abandoned: its Hamming distance
depends on how many features a document has, so a fixed bit threshold means
something different on a 60-word page than on a 600-word one. Measured, adding
three words to a 60-word document moved 7 bits, while the same edit on a
150-word document moved 2 — so any single threshold is either blind on short
pages or hysterical on long ones.

A bottom-k MinHash sketch estimates Jaccard similarity instead, which is a
proportion and therefore length-independent. It is also directly reportable:
"these two pages are 92% identical" is a sentence someone can act on, where
"7 bits apart" is not.
"""

from __future__ import annotations

import hashlib
import re

SHINGLE_SIZE = 4          # words per shingle
SKETCH_SIZE = 128         # k, in bottom-k; ~±5% accuracy on the Jaccard estimate

# Below this a sketch is mostly noise, and thin pages are the thin-content
# check's problem rather than the duplicate check's.
MIN_WORDS = 25

# Calibrated, not guessed. Measured across a crawl of wordpress.org: genuinely
# unrelated pages on the same site sit at 10% median similarity and 16% at the
# 90th percentile, while the one real near-duplicate pair — two almost-identical
# privacy request forms — measured 62%. Two product pages differing only in price
# and colour measured 80%. 60% sits well clear of the unrelated band while still
# catching both. Tunable via Thresholds.near_duplicate_similarity.
NEAR_DUPLICATE_SIMILARITY = 0.60

_WORD = re.compile(r"\w+", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def shingles(text: str, size: int = SHINGLE_SIZE) -> list[str]:
    words = _WORD.findall(text.lower())
    if len(words) < size:
        return [" ".join(words)] if words else []
    return [" ".join(words[i:i + size]) for i in range(len(words) - size + 1)]


def content_hash(text: str) -> str:
    """Exact fingerprint, insensitive to whitespace and case."""
    normalized = _WHITESPACE.sub(" ", text.strip().lower())
    if len(_WORD.findall(normalized)) < MIN_WORDS:
        return ""
    return hashlib.blake2b(normalized.encode("utf-8"), digest_size=16).hexdigest()


def _hash(feature: str) -> int:
    return int.from_bytes(hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest(), "big")


def sketch(text: str, k: int = SKETCH_SIZE) -> tuple[int, ...]:
    """The k smallest shingle hashes. Empty when the text is too short to judge."""
    if len(_WORD.findall(text)) < MIN_WORDS:
        return ()
    features = shingles(text)
    if not features:
        return ()
    return tuple(sorted({_hash(feature) for feature in features})[:k])


def similarity(left: tuple[int, ...], right: tuple[int, ...], k: int = SKETCH_SIZE) -> float:
    """Estimated Jaccard similarity, 0.0 to 1.0.

    The k-minimum-values estimator: take the k smallest hashes of the union, and
    ask what fraction of them appear in both sketches.
    """
    if not left or not right:
        return 0.0
    union = sorted(set(left) | set(right))[:k]
    if not union:
        return 0.0
    shared = set(left) & set(right)
    return sum(1 for value in union if value in shared) / len(union)


def near_duplicate(left: tuple[int, ...], right: tuple[int, ...],
                   threshold: float = NEAR_DUPLICATE_SIMILARITY) -> bool:
    return similarity(left, right) >= threshold
