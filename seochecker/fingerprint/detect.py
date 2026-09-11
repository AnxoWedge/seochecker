"""The fingerprint rules engine.

Signals are evidence, not verdicts. Each one carries a confidence, and
independent signals combine — two mediocre signals that agree beat one good one.
That is the whole reason this is a rules engine and not a pile of `if`s: adding a
technology means adding YAML, and the scoring stays consistent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator

import yaml

from ..html import Document
from ..models import Page

RULES_PATH = Path(__file__).parent / "rules.yaml"

DEFAULT_CONFIDENCE = 0.7
MIN_REPORTED_CONFIDENCE = 0.5
# An implied technology is only as certain as what implied it, and a little less.
IMPLICATION_DECAY = 0.9

SIGNAL_TYPES = frozenset({
    "header", "cookie", "meta", "html", "script", "stylesheet", "url", "dom", "js",
})


_META = frozenset(".^$*+?{}[]|()")
_ESCAPABLE = frozenset(".^$*+?{}[]|()/-\\")
MIN_LITERAL = 4


def longest_literal(pattern: str) -> str:
    """Longest substring the pattern definitely requires, or '' if there is none.

    Used as a pre-filter: `substring in html` runs at C speed, while the regex
    engine over a 700 KB document does not. Correctness rests on being
    conservative — only top-level runs count, anything optional is dropped, and a
    top-level alternation disqualifies the pattern entirely. A wrong '' costs a
    little speed; a wrong literal would silently lose detections.
    """
    runs: list[str] = []
    current: list[str] = []
    depth = 0
    index = 0
    size = len(pattern)

    def flush() -> None:
        runs.append("".join(current))
        current.clear()

    while index < size:
        char = pattern[index]

        if char == "\\":
            following = pattern[index + 1] if index + 1 < size else ""
            if following in _ESCAPABLE and depth == 0:
                current.append(following)
            else:
                flush()          # \d, \w, \s — a class, not a literal
            index += 2
            continue

        if char == "[":          # character class: skip it whole
            flush()
            index += 1
            while index < size and pattern[index] != "]":
                index += 2 if pattern[index] == "\\" else 1
            index += 1
            continue

        if char == "{":          # {0,3} can match nothing, so drop what it governs
            if current:
                current.pop()
            flush()
            while index < size and pattern[index] != "}":
                index += 1
            index += 1
            continue

        if char in "*?":         # governs the previous character, which is optional
            if current:
                current.pop()
            flush()
            index += 1
            continue

        if char == "(":
            flush()
            depth += 1
            index += 1
            continue

        if char == ")":
            flush()
            depth = max(0, depth - 1)
            index += 1
            continue

        if char == "|":
            if depth == 0:
                return ""        # nothing is guaranteed across a top-level branch
            flush()
            index += 1
            continue

        if char in _META:        # . ^ $ +
            flush()
            index += 1
            continue

        if depth == 0:
            current.append(char)
        index += 1

    flush()
    best = max(runs, key=len, default="")
    return best.lower() if len(best) >= MIN_LITERAL else ""


class RulesError(ValueError):
    """The rules file is malformed — a bug in the catalogue, not in a site."""


@dataclass(slots=True)
class Signal:
    type: str
    confidence: float
    key: str | None = None
    key_re: re.Pattern[str] | None = None
    pattern: re.Pattern[str] | None = None
    selector: str | None = None
    attribute: str | None = None
    version_group: int | None = None
    literal: str = ""   # cheap pre-filter for `html` signals

    def version_from(self, match: re.Match[str] | None) -> str:
        if match is None or self.version_group is None:
            return ""
        try:
            return (match.group(self.version_group) or "").strip()
        except IndexError:  # a rule names a capture group its pattern does not have
            return ""


@dataclass(slots=True)
class Technology:
    name: str
    category: str
    website: str = ""
    implies: list[str] = field(default_factory=list)
    signals: list[Signal] = field(default_factory=list)


@dataclass(slots=True)
class Detection:
    name: str
    category: str
    confidence: float
    version: str = ""
    website: str = ""
    evidence: list[str] = field(default_factory=list)
    implied_by: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category,
            "confidence": self.confidence,
            "version": self.version or None,
            "website": self.website or None,
            "evidence": self.evidence,
            "implied_by": self.implied_by or None,
        }


def _compile(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.I)


def load_rules(path: Path | None = None) -> tuple[dict[str, Technology], dict[str, str]]:
    """Parse and validate rules.yaml into compiled Technology objects."""
    path = path or RULES_PATH
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    categories: dict[str, str] = data.get("categories", {})
    technologies: dict[str, Technology] = {}

    for name, spec in (data.get("technologies") or {}).items():
        category = spec.get("category", "")
        if category not in categories:
            raise RulesError(f"{name}: unknown category {category!r}")

        signals: list[Signal] = []
        for raw in spec.get("signals") or []:
            kind = raw.get("type")
            if kind not in SIGNAL_TYPES:
                raise RulesError(f"{name}: unknown signal type {kind!r}")
            try:
                signals.append(
                    Signal(
                        type=kind,
                        confidence=float(raw.get("confidence", DEFAULT_CONFIDENCE)),
                        key=raw.get("key") if kind in ("header", "meta", "js") else None,
                        key_re=_compile(raw["key"]) if kind == "cookie" else None,
                        pattern=_compile(raw["pattern"]) if raw.get("pattern") else None,
                        selector=raw.get("selector"),
                        attribute=raw.get("attribute"),
                        version_group=raw.get("version"),
                        literal=(longest_literal(raw["pattern"])
                                 if kind == "html" and raw.get("pattern") else ""),
                    )
                )
            except re.error as exc:
                raise RulesError(f"{name}: bad regex — {exc}") from exc
            if kind == "dom" and not raw.get("selector"):
                raise RulesError(f"{name}: dom signal needs a selector")

        technologies[name] = Technology(
            name=name,
            category=category,
            website=spec.get("website", ""),
            implies=list(spec.get("implies") or []),
            signals=signals,
        )

    for name, tech in technologies.items():
        for implied in tech.implies:
            if implied not in technologies:
                raise RulesError(f"{name} implies unknown technology {implied!r}")

    return technologies, categories


@lru_cache(maxsize=4)
def _cached_rules(path: str) -> tuple[dict[str, Technology], dict[str, str]]:
    return load_rules(Path(path))


def _combine(confidences: list[float]) -> float:
    """Noisy-or: independent evidence accumulates but never quite reaches certainty.

    Two 0.7 signals give 0.91, three give 0.97 — which is what "several weak
    signals agreeing" should feel like.
    """
    remaining = 1.0
    for value in confidences:
        remaining *= 1.0 - min(max(value, 0.0), 1.0)
    return round(1.0 - remaining, 3)


class Fingerprinter:
    """Runs the rule catalogue against one fetched page."""

    def __init__(self, rules_path: Path | None = None,
                 min_confidence: float = MIN_REPORTED_CONFIDENCE) -> None:
        self.technologies, self.categories = _cached_rules(str(rules_path or RULES_PATH))
        self.min_confidence = min_confidence

    # --- signal evaluation -------------------------------------------------

    def _check(self, signal: Signal, page: Page, doc: Document | None
               ) -> tuple[bool, str, str]:
        """Return (matched, version, evidence)."""
        match signal.type:
            case "header":
                value = page.headers.get((signal.key or "").lower(), "")
                if not value:
                    return False, "", ""
                found = signal.pattern.search(value) if signal.pattern else None
                if signal.pattern and not found:
                    return False, "", ""
                return True, signal.version_from(found), f"header {signal.key}: {value[:80]}"

            case "cookie":
                for name, value in page.cookies.items():
                    if signal.key_re and signal.key_re.search(name):
                        if signal.pattern and not signal.pattern.search(value):
                            continue
                        return True, "", f"cookie {name}"
                return False, "", ""

            case "js":
                # Only available when the page was rendered; absent otherwise,
                # which means "unknown", not "no".
                if signal.key in page.js_globals:
                    return True, "", f"js global: {signal.key}"
                return False, "", ""

            case "url":
                found = signal.pattern.search(page.final_url) if signal.pattern else None
                if found:
                    return True, signal.version_from(found), f"url: {page.final_url[:80]}"
                return False, "", ""

        if doc is None or signal.pattern is None and signal.type != "dom":
            return False, "", ""

        match signal.type:
            case "meta":
                for content in doc.meta_all(signal.key or ""):
                    found = signal.pattern.search(content) if signal.pattern else None
                    if found:
                        return True, signal.version_from(found), \
                               f"meta {signal.key}: {content[:80]}"
                return False, "", ""

            case "html":
                if signal.literal and signal.literal not in doc.lower_raw:
                    return False, "", ""
                found = signal.pattern.search(doc.raw)
                if found:
                    return True, signal.version_from(found), f"html: {found.group(0)[:60]}"
                return False, "", ""

            case "script":
                for script in doc.scripts:
                    if script.src and (found := signal.pattern.search(script.src)):
                        return True, signal.version_from(found), f"script: {script.src[:80]}"
                return False, "", ""

            case "stylesheet":
                for href in doc.stylesheets:
                    if found := signal.pattern.search(href):
                        return True, signal.version_from(found), f"stylesheet: {href[:80]}"
                return False, "", ""

            case "dom":
                node = doc.tree.css_first(signal.selector or "")
                if node is None:
                    return False, "", ""
                if signal.attribute:
                    value = node.attributes.get(signal.attribute) or ""
                    found = signal.pattern.search(value) if signal.pattern else None
                    if signal.pattern and not found:
                        return False, "", ""
                    return True, signal.version_from(found), \
                           f"dom {signal.selector}[{signal.attribute}={value[:40]}]"
                return True, "", f"dom: {signal.selector}"

        return False, "", ""

    # --- detection ---------------------------------------------------------

    def js_globals(self) -> list[str]:
        """Every JavaScript global the rules ask about, for the renderer to probe."""
        return sorted({
            signal.key
            for tech in self.technologies.values()
            for signal in tech.signals
            if signal.type == "js" and signal.key
        })

    def detect(self, page: Page, doc: Document | None) -> list[Detection]:
        direct: dict[str, Detection] = {}

        for tech in self.technologies.values():
            hits: list[tuple[float, str, str]] = []
            for signal in tech.signals:
                matched, version, evidence = self._check(signal, page, doc)
                if matched:
                    hits.append((signal.confidence, version, evidence))
            if not hits:
                continue

            confidence = _combine([c for c, _, _ in hits])
            if confidence < self.min_confidence:
                continue
            hits.sort(key=lambda hit: hit[0], reverse=True)
            direct[tech.name] = Detection(
                name=tech.name,
                category=tech.category,
                confidence=confidence,
                version=next((v for _, v, _ in hits if v), ""),
                website=tech.website,
                evidence=[evidence for _, _, evidence in hits],
            )

        return self._sorted(self._apply_implications(direct))

    def _apply_implications(self, direct: dict[str, Detection]) -> dict[str, Detection]:
        """WooCommerce means WordPress means PHP — resolved transitively."""
        results = dict(direct)
        queue = list(direct.values())
        while queue:
            parent = queue.pop()
            for name in self.technologies[parent.name].implies:
                confidence = round(parent.confidence * IMPLICATION_DECAY, 3)
                existing = results.get(name)
                if existing and existing.implied_by == "":
                    # A direct detection keeps its own evidence, but a strong
                    # implication can still raise how certain we are of it.
                    if confidence > existing.confidence:
                        existing.confidence = confidence
                        existing.evidence.append(f"implied by {parent.name}")
                    continue
                if existing and existing.confidence >= confidence:
                    continue
                tech = self.technologies[name]
                implied = Detection(
                    name=name,
                    category=tech.category,
                    confidence=confidence,
                    website=tech.website,
                    evidence=[f"implied by {parent.name}"],
                    implied_by=parent.name,
                )
                results[name] = implied
                queue.append(implied)
        return results

    @staticmethod
    def _sorted(detections: dict[str, Detection]) -> list[Detection]:
        return sorted(detections.values(),
                      key=lambda d: (-d.confidence, d.category, d.name))


def group_by_category(detections: list[Detection]) -> dict[str, list[Detection]]:
    grouped: dict[str, list[Detection]] = {}
    for detection in detections:
        grouped.setdefault(detection.category, []).append(detection)
    return grouped
