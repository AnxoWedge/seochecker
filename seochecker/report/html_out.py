"""The HTML report.

Self-contained on purpose: no CDN, no fonts, no network. It has to work from a
file:// URL, in an email attachment, and on a laptop with no internet — which is
how a client will actually open it.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .. import __version__
from ..models import Finding, Page, Severity

TEMPLATE = Path(__file__).parent / "template.html.j2"

SEVERITY_ORDER = {"critical": 0, "warning": 1, "notice": 2, "info": 3}


def _shorten(url: str, limit: int = 68) -> str:
    """Trim the scheme and host so the distinguishing part of a URL stays visible."""
    parts = urlsplit(str(url))
    text = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    if not parts.netloc:
        text = str(url)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def group_findings(pages: list[Page]) -> list[dict[str, Any]]:
    """One entry per distinct finding id, carrying the URLs it applies to."""
    buckets: dict[str, dict[str, Any]] = {}
    for page in pages:
        for finding in page.findings:
            entry = buckets.get(finding.id)
            if entry is None:
                entry = buckets[finding.id] = {
                    "id": finding.id,
                    "severity": finding.severity.value,
                    "category": finding.category,
                    "message": finding.message,
                    "evidence": finding.evidence,
                    "fix": finding.fix,
                    "urls": [],
                }
            entry["urls"].append(finding.url or page.final_url)
    ordered = sorted(
        buckets.values(),
        key=lambda e: (SEVERITY_ORDER.get(e["severity"], 9), -len(e["urls"]), e["id"]),
    )
    return ordered


def build_context(
    *,
    target: str,
    pages: list[Page],
    site_findings: list[Finding],
    score: Any = None,
    technologies: list[Any] | None = None,
    graph: dict[str, Any] | None = None,
    crawl: dict[str, Any] | None = None,
    stats: dict[str, Any] | None = None,
    stopped_because: str = "",
    comparison: Any = None,
    external_data: list | None = None,
) -> dict[str, Any]:
    every = list(site_findings) + [f for page in pages for f in page.findings]
    counts = Counter(f.severity.value for f in every)

    rows = []
    for page in pages:
        by_severity = Counter(f.severity for f in page.findings)
        rows.append({
            "final_url": page.final_url,
            "status": page.status,
            "error": page.error.value if page.error else None,
            "ok": page.ok,
            "click_depth": page.click_depth,
            "inlink_count": page.inlink_count,
            "pagerank": page.pagerank,
            "seo": page.seo or None,
            "findings": page.findings,
            "critical": by_severity[Severity.CRITICAL],
            "warning": by_severity[Severity.WARNING],
        })
    rows.sort(key=lambda r: (-r["critical"], -r["warning"], r["final_url"]))

    return {
        "version": __version__,
        "target": target,
        "host": urlsplit(target).netloc or target,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "pages": rows,
        "ok_pages": sum(1 for r in rows if r["ok"]),
        "counts": {level: counts.get(level, 0)
                   for level in ("critical", "warning", "notice", "info")},
        "site_findings": [
            {"id": f.id, "severity": f.severity.value, "message": f.message,
             "evidence": f.evidence, "fix": f.fix}
            for f in sorted(site_findings,
                            key=lambda f: (SEVERITY_ORDER.get(f.severity.value, 9), f.id))
        ],
        "grouped": group_findings(pages),
        "score": score.to_dict() if score is not None and hasattr(score, "to_dict") else score,
        "technologies": [t.to_dict() if hasattr(t, "to_dict") else t
                         for t in (technologies or [])],
        "graph": graph or {},
        "crawl": crawl or {},
        "stats": stats or {},
        "stopped_because": stopped_because,
        "vitals": [{"url": p.final_url, **p.vitals} for p in pages if p.vitals],
        "external_data": external_data or [],
        "comparison": comparison.to_dict() if comparison is not None else None,
        "comparison_metrics": comparison.metric_table() if comparison is not None else [],
        "comparison_hosts": ([comparison.target.host] + [r.host for r in comparison.rivals]
                             if comparison is not None else []),
        "comparison_scores": ([{"host": s.host, "score": s.score, "grade": s.grade}
                               for s in comparison.sites]
                              if comparison is not None else []),
    }


def render_html(context: dict[str, Any]) -> str:
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE.parent)),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["short"] = _shorten
    return env.get_template(TEMPLATE.name).render(**context)


def write_html(path: str | Path, context: dict[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_html(context), encoding="utf-8")
    return path
