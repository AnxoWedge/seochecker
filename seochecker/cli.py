"""Command-line entrypoint.

Phase 2 runs the analyzer suite over the fetched page. The report is shaped as a
list of pages so Phase 4's crawler can fill it without the schema changing.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .analyzers import PageContext, run_page_analyzers
from .config import CrawlConfig, config_from_args
from .fetch import Fetcher
from .fingerprint import Detection, Fingerprinter, group_by_category
from .html import Document
from .models import Finding, Page, Severity
from .thresholds import Thresholds

MISSING = "[red]— missing —[/red]"

SEVERITY_STYLE = {
    Severity.CRITICAL: ("CRITICAL", "bold white on red"),
    Severity.WARNING: ("WARNING ", "bold black on yellow"),
    Severity.NOTICE: ("NOTICE  ", "bold black on cyan"),
    Severity.INFO: ("INFO    ", "bold white on blue"),
}
SEVERITY_RANK = {Severity.CRITICAL: 0, Severity.WARNING: 1, Severity.NOTICE: 2, Severity.INFO: 3}


async def audit_single(
    config: CrawlConfig,
) -> tuple[Page, dict[str, Any] | None, list[Detection], dict[str, Any]]:
    started = time.perf_counter()
    async with Fetcher(config) as fetcher:
        page = await fetcher.fetch(config.url)
        stats = {
            "requests": fetcher.requests_made,
            "bytes_downloaded": fetcher.bytes_downloaded,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        }

    # A challenged or failed fetch has no page worth parsing — analysing the
    # mitigation page would report a dozen fictional SEO problems.
    doc = (
        Document(page.html, page.final_url)
        if page.error is None and page.is_html and page.html
        else None
    )
    ctx = PageContext(page=page, doc=doc, config=config, thresholds=Thresholds())
    fingerprinter = Fingerprinter(
        Path(config.rules) if config.rules else None,
        min_confidence=config.min_confidence,
    )
    technologies = [] if page.error else fingerprinter.detect(page, doc)
    ctx.technologies = technologies
    page.findings = run_page_analyzers(ctx)
    return page, (doc.summary() if doc else None), technologies, stats


def summarise(findings: list[Finding]) -> dict[str, Any]:
    by_severity = Counter(f.severity.value for f in findings)
    return {
        "total": len(findings),
        "by_severity": {s.value: by_severity.get(s.value, 0) for s in Severity},
        "by_category": dict(Counter(f.category for f in findings).most_common()),
        "ids": sorted({f.id for f in findings}),
    }


def build_report(config: CrawlConfig, page: Page, seo: dict[str, Any] | None,
                 technologies: list[Detection], stats: dict[str, Any]) -> dict[str, Any]:
    record = page.to_dict(include_html=config.include_html)
    record["seo"] = seo
    return {
        "seochecker": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target": config.url,
        "mode": "single",
        "config": {
            "user_agent": config.user_agent,
            "http2": config.http2,
            "timeout": config.timeout,
            "max_retries": config.max_retries,
            "delay": config.delay,
            "obey_robots": config.obey_robots,
        },
        "stats": stats,
        "summary": summarise(page.findings),
        "technologies": [tech.to_dict() for tech in technologies],
        "pages": [record],
    }


def render_findings(console, findings: list[Finding], minimum: Severity) -> None:
    from rich.padding import Padding
    from rich.text import Text

    # Wrapped evidence and fix lines hang under the message, not under the badge.
    indent = 11

    shown = [f for f in findings if SEVERITY_RANK[f.severity] <= SEVERITY_RANK[minimum]]
    if not shown:
        console.print("\n[green]No findings at or above this severity.[/green]")
        return

    console.print(f"\n[bold]Findings[/bold] [dim]({len(shown)} shown)[/dim]")
    for finding in shown:
        label, style = SEVERITY_STYLE[finding.severity]
        line = Text.assemble(
            (f" {label} ", style), "  ",
            (finding.id, "bold"), "  ",
            finding.message,
        )
        console.print(line)
        if finding.evidence:
            console.print(Padding(Text(finding.evidence, style="dim"), (0, 0, 0, indent)))
        if finding.fix:
            console.print(Padding(Text(f"→ {finding.fix}", style="dim italic"),
                                  (0, 0, 0, indent)))


def render_technologies(console, technologies: list[Detection]) -> None:
    from rich.table import Table

    if not technologies:
        console.print("\n[dim]No technologies identified.[/dim]")
        return

    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    table.add_column(style="dim", width=15)
    table.add_column(overflow="fold")
    for category, items in group_by_category(technologies).items():
        rendered = []
        for tech in items:
            name = f"{tech.name} {tech.version}".strip()
            mark = "[dim]~[/dim]" if tech.implied_by else ""
            confidence = f"[dim]{tech.confidence:.0%}[/dim]"
            rendered.append(f"{mark}{name} {confidence}")
        table.add_row(category, " · ".join(rendered))
    console.print("\n[bold]Technology[/bold] [dim](~ = inferred)[/dim]")
    console.print(table)


def render_human(page: Page, seo: dict[str, Any] | None, technologies: list[Detection],
                 stats: dict[str, Any], minimum: Severity) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console(stderr=True)

    if page.error and page.status is None:
        console.print(
            f"[bold red]FAILED[/bold red] {page.requested_url}\n"
            f"  {page.error.value}: {page.error_detail}"
        )
        return

    status_colour = "green" if page.ok else "yellow" if page.status and page.status < 400 else "red"
    console.print(
        f"\n[bold]{page.final_url}[/bold]  "
        f"[{status_colour}]{page.status} {page.reason}[/{status_colour}]  "
        f"[dim]{page.http_version} · {page.timing.ttfb_ms:.0f}ms TTFB · "
        f"{page.wire_bytes / 1024:.1f} KB[/dim]"
    )
    if page.error:
        console.print(f"[red]{page.error.value}: {page.error_detail}[/red]")

    if page.redirects:
        for hop in page.redirects:
            console.print(f"  [dim]{hop.status}[/dim] {hop.url} [dim]->[/dim] {hop.location}")

    if seo:
        facts = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
        facts.add_column(style="dim", width=15)
        facts.add_column(overflow="fold")
        facts.add_row("title", f"{seo['title']} [dim]({seo['title_length']})[/dim]"
                      if seo["title"] else MISSING)
        facts.add_row("description", f"{seo['description']} [dim]({seo['description_length']})[/dim]"
                      if seo["description"] else MISSING)
        facts.add_row("canonical", seo["canonical"] or MISSING)
        facts.add_row("h1", " · ".join(seo["h1"]) if seo["h1"] else MISSING)
        facts.add_row("lang / server", f"{seo['lang'] or '—'} · {page.header('server') or '—'}")
        facts.add_row("content", f"{seo['word_count']} words · "
                                 f"{seo['links_total']} links "
                                 f"({seo['links_internal']} internal) · "
                                 f"{seo['images_total']} images "
                                 f"({seo['images_missing_alt']} without alt)")
        facts.add_row("structured", ", ".join(seo["json_ld_types"]) or "[dim]none[/dim]")
        console.print()
        console.print(facts)
    elif not page.error:
        console.print(f"\n[dim]No HTML to analyse (mime: {page.mime or 'unknown'}).[/dim]")

    if seo:
        render_technologies(console, technologies)

    render_findings(console, page.findings, minimum)

    counts = Counter(f.severity for f in page.findings)
    parts = [
        f"[red]{counts[Severity.CRITICAL]} critical[/red]",
        f"[yellow]{counts[Severity.WARNING]} warning[/yellow]",
        f"[cyan]{counts[Severity.NOTICE]} notice[/cyan]",
        f"[blue]{counts[Severity.INFO]} info[/blue]",
    ]
    console.print(f"\n{' · '.join(parts)}   [dim]{stats['requests']} request(s), "
                  f"{stats['bytes_downloaded'] / 1024:.1f} KB, {stats['duration_ms']:.0f}ms[/dim]\n")


def exit_code(page: Page, fail_on: str) -> int:
    if page.error is not None or not page.ok:
        return 1
    if fail_on == "never":
        return 0
    threshold = SEVERITY_RANK[Severity(fail_on)]
    if any(SEVERITY_RANK[f.severity] <= threshold for f in page.findings):
        return 1
    return 0 if page.ok else 1


def main(argv: list[str] | None = None) -> int:
    config = config_from_args(argv)

    if not config.single:
        print(
            "note: multi-page crawling arrives in Phase 4 — auditing the single URL.",
            file=sys.stderr,
        )

    try:
        page, seo, technologies, stats = asyncio.run(audit_single(config))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    report = build_report(config, page, seo, technologies, stats)
    payload = json.dumps(report, indent=2, ensure_ascii=False)

    if config.out:
        path = Path(config.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload + "\n", encoding="utf-8")
        if not config.quiet:
            print(f"wrote {path}", file=sys.stderr)
    else:
        print(payload)

    if not config.quiet:
        render_human(page, seo, technologies, stats, Severity(config.min_severity))

    return exit_code(page, config.fail_on)
