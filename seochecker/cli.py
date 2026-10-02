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
from urllib.parse import urlsplit

from . import __version__
from .analyzers import PageContext, SiteContext, run_page_analyzers, run_site_analyzers
from .compare import Comparison, SiteMetrics
from .crawl import CrawlResult, Crawler
from .config import CrawlConfig, config_from_args
from .fetch import Fetcher
from .fingerprint import Detection, Fingerprinter, group_by_category
from .graph import LinkGraph
from .html import Document
from . import language
from .models import Finding, Page, Severity
from .providers import configured as configured_providers
from .report import RunStore, diff_runs, write_csv, write_html
from .report.html_out import build_context
from .robots import product_token
from .score import Scorecard, evaluated_categories, score
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


async def query_providers(config: CrawlConfig, result: CrawlResult) -> None:
    """Ask the configured external providers about this site.

    Only providers with credentials are called. The rest are recorded as
    unavailable with what they need, so an empty section explains itself.
    """
    host = urlsplit(config.url).netloc
    for provider in configured_providers(psi_key=config.psi_key, opr_key=config.opr_key):
        if not provider.available:
            result.external_data.append(
                {"provider": provider.name, "available": False, "free": provider.free,
                 "requires": provider.requires})
            continue
        # PageSpeed Insights answers per URL; Open PageRank per domain.
        answer = (await provider.page_metrics(config.url)
                  if provider.name == "PageSpeed Insights"
                  else await provider.domain_metrics(host))
        result.external_data.append({**answer.to_dict(), "available": True,
                                     "free": provider.free})


async def run_crawl(config: CrawlConfig, on_page=None, should_stop=None) -> CrawlResult:
    crawler = Crawler(config, on_page=on_page, should_stop=should_stop)
    result = await crawler.run()
    if config.psi_key or config.opr_key:
        await query_providers(config, result)
    result.site_findings = run_site_analyzers(
        SiteContext(
            config=config,
            pages=result.pages,
            robots=result.robots,
            sitemap=result.sitemap,
            llms_txt=result.llms_txt,
            sitemap_urls=set(result.frontier.get("sitemap_urls", [])),
            frontier=result.frontier,
            technologies=result.technologies,
            thresholds=Thresholds(),
            graph=result.graph,
            languages=language.build(result.pages),
            soft_404_fingerprint=result.soft_404_fingerprint,
            external_links=result.external_links,
        )
    )
    return result


def all_findings(result: CrawlResult) -> list[Finding]:
    findings = list(result.site_findings)
    for page in result.pages:
        findings.extend(page.findings)
    return findings


def scorecard(config: CrawlConfig, result: CrawlResult) -> Scorecard:
    return score(
        all_findings(result),
        pages=len(result.pages),
        categories=evaluated_categories(
            crawled=not config.single,
            rendered=bool(result.stats.get("rendered")),
            external=bool(result.external_links),
        ),
    )


async def run_comparison(config: CrawlConfig, on_page=None,
                         should_stop=None) -> tuple[CrawlResult, Scorecard, Comparison]:
    """Crawl the target and every rival to the same budget, then compare.

    The rival crawls run concurrently because they are different hosts — pacing is
    per-host, so nobody is asked for more than they would be in a solo crawl.
    """
    from dataclasses import replace

    configs = [config] + [replace(config, url=rival, against=[]) for rival in config.against]
    results = await asyncio.gather(*(
        run_crawl(cfg, on_page=on_page if index == 0 else None, should_stop=should_stop)
        for index, cfg in enumerate(configs)
    ))

    cards = [scorecard(cfg, result) for cfg, result in zip(configs, results)]
    metrics = [SiteMetrics.from_result(result, card, config.max_pages)
               for result, card in zip(results, cards)]
    # SiteMetrics reads the target URL off the first page, which a failed crawl
    # does not have; fall back to what was asked for.
    for site, cfg in zip(metrics, configs):
        if not site.url:
            site.url = cfg.url
            site.host = urlsplit(cfg.url).netloc

    comparison = Comparison(target=metrics[0], rivals=metrics[1:], budget=config.max_pages)
    return results[0], cards[0], comparison


def build_crawl_report(config: CrawlConfig, result: CrawlResult,
                       card: Scorecard | None = None,
                       comparison: Comparison | None = None) -> dict[str, Any]:
    return {
        "seochecker": __version__,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "target": config.url,
        "mode": "crawl",
        "config": {
            "user_agent": config.user_agent,
            "max_pages": config.max_pages,
            "max_depth": config.max_depth,
            "concurrency": config.concurrency,
            "delay": config.delay,
            "obey_robots": config.obey_robots,
            "use_sitemap": config.use_sitemap,
            "include_subdomains": config.include_subdomains,
        },
        "stats": result.stats,
        "crawl": {
            "pages_crawled": len(result.pages),
            "stopped_because": result.stopped_because or "frontier exhausted",
            "frontier": {k: v for k, v in result.frontier.items() if k != "sitemap_urls"},
            "robots": {
                "url": result.robots.source_url,
                "fetched": result.robots.fetched,
                "status": result.robots.status,
                "error": result.robots.error or None,
                "sitemaps": result.robots.sitemaps,
                "crawl_delay": result.robots.crawl_delay(product_token(config.user_agent)),
            },
            "sitemap": {
                "files": result.sitemap.fetched,
                "failed": [{"url": u, "reason": r} for u, r in result.sitemap.failed],
                "urls": len(result.sitemap.entries),
                "truncated": result.sitemap.truncated,
            },
        },
        "score": card.to_dict() if card else None,
        "comparison": comparison.to_dict() if comparison else None,
        "summary": summarise(all_findings(result)),
        "external_data": result.external_data or None,
        "vitals": [
            {"url": p.final_url, **p.vitals} for p in result.pages if p.vitals
        ] or None,
        "graph": result.graph.summary(),
        "technologies": [tech.to_dict() for tech in result.technologies],
        "site_findings": [_finding_dict(f) for f in result.site_findings],
        "pages": [p.to_dict(include_html=config.include_html,
                            include_links=config.include_links) for p in result.pages],
    }


def _finding_dict(finding: Finding) -> dict[str, Any]:
    from dataclasses import asdict
    return {**asdict(finding), "severity": finding.severity.value}


def summarise(findings: list[Finding]) -> dict[str, Any]:
    by_severity = Counter(f.severity.value for f in findings)
    return {
        "total": len(findings),
        "by_severity": {s.value: by_severity.get(s.value, 0) for s in Severity},
        "by_category": dict(Counter(f.category for f in findings).most_common()),
        "by_id": dict(Counter(f.id for f in findings).most_common()),
    }


def single_result(page: Page, technologies: list[Detection],
                  stats: dict[str, Any]) -> CrawlResult:
    """Wrap a one-page audit as a CrawlResult, so it can use the same reporting.

    Without this, --html, --csv and --db were accepted in single mode and
    silently did nothing.
    """
    result = CrawlResult(pages=[page], technologies=technologies, stats=stats)
    result.graph = LinkGraph.build([page], page.final_url)
    result.graph.apply_to([page])
    return result


def single_scorecard(page: Page) -> Scorecard:
    return score(
        page.findings,
        pages=1,
        categories=evaluated_categories(crawled=False, rendered=page.rendered,
                                        external=False),
    )


def build_report(config: CrawlConfig, page: Page, seo: dict[str, Any] | None,
                 technologies: list[Detection], stats: dict[str, Any],
                 card: Scorecard | None = None) -> dict[str, Any]:
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
        "score": card.to_dict() if card else None,
        "summary": summarise(page.findings),
        "technologies": [tech.to_dict() for tech in technologies],
        "pages": [record],
    }


def render_findings(console, findings: list[Finding], minimum: Severity,
                    title: str = "Findings") -> None:
    from rich.padding import Padding
    from rich.text import Text

    # Wrapped evidence and fix lines hang under the message, not under the badge.
    indent = 11

    shown = [f for f in findings if SEVERITY_RANK[f.severity] <= SEVERITY_RANK[minimum]]
    if not shown:
        console.print("\n[green]No findings at or above this severity.[/green]")
        return

    console.print(f"\n[bold]{title}[/bold] [dim]({len(shown)} shown)[/dim]")
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


def render_crawl(result: CrawlResult, config: CrawlConfig, minimum: Severity,
                 card: Scorecard | None = None) -> None:
    """Report what to improve, grouped by what improving it would buy you."""
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    from .lenses import ALL_LENSES, LABELS

    console = Console(stderr=True)
    pages = [p for p in result.pages if not p.duplicate_of]
    ok_pages = [p for p in pages if p.error is None and p.ok]
    duration = result.stats.get("duration_ms", 0) / 1000

    # --- headline ----------------------------------------------------------
    console.print(f"\n[bold]{config.url}[/bold]  "
                  f"[dim]{len(pages)} pages in {duration:.0f}s · "
                  f"{len(ok_pages)}/{len(pages)} returned 200[/dim]")

    if card:
        def tint(value: float) -> str:
            return "green" if value >= 85 else "yellow" if value >= 65 else "red"

        parts = [f"[bold {tint(card.overall)}]{card.overall:.0f}/100[/bold {tint(card.overall)}]"
                 f" [dim]grade {card.grade}[/dim]"]
        for lens in ALL_LENSES:
            value = card.lenses.get(lens)
            if value is None:
                continue
            if lens == "performance" and not result.vitals_measured:
                parts.append(f"[dim]{LABELS[lens]} not measured (--vitals)[/dim]")
                continue
            parts.append(f"{LABELS[lens]} [{tint(value)}]{value:.0f}[/{tint(value)}]")
        console.print("  " + "   ".join(parts))

    # --- context, in two lines rather than three panels ---------------------
    robots = result.robots
    crawl_bits = [
        "robots.txt " + ("found" if robots.fetched else str(robots.error or robots.status or "none")),
        (f"sitemap {len(result.sitemap.entries)} URLs" if result.sitemap.fetched
         else "[yellow]no sitemap[/yellow]"),
    ]
    if result.graph.nodes:
        summary = result.graph.summary()
        crawl_bits.append(f"{summary['edges']} internal links")
        if summary["orphans"]:
            crawl_bits.append(f"[yellow]{summary['orphans']} orphans[/yellow]")
    if result.stats.get("rendered"):
        crawl_bits.append(f"{result.stats['rendered']} rendered")
    if result.vitals_measured:
        crawl_bits.append(f"{result.vitals_measured} measured")
    console.print(f"  [dim]{' · '.join(crawl_bits)}[/dim]")

    if result.technologies:
        shown = [t for t in result.technologies if not t.implied_by][:7]
        stack = " · ".join(f"{t.name}{' ' + t.version if t.version else ''}" for t in shown)
        console.print(f"  [dim]stack: {stack}[/dim]")

    # --- findings, grouped by what fixing them buys --------------------------
    findings = list(result.site_findings) + [f for p in pages for f in p.findings]
    if not findings:
        console.print("\n[green]Nothing to report.[/green]\n")
        return

    grouped: dict[str, dict[str, list[Finding]]] = {lens: {} for lens in ALL_LENSES}
    for finding in findings:
        if SEVERITY_RANK[finding.severity] > SEVERITY_RANK[minimum]:
            continue
        grouped.setdefault(finding.lens, {}).setdefault(finding.id, []).append(finding)

    # Laid out by hand rather than with a table: a table shrinks its columns to
    # fit and silently truncated the severity labels to "W…".
    severity_tag = {
        Severity.CRITICAL: "[bold red]CRIT[/bold red]",
        Severity.WARNING: "[yellow]WARN[/yellow]",
        Severity.NOTICE: "[cyan]note[/cyan]",
        Severity.INFO: "[dim]info[/dim]",
    }

    for lens in ALL_LENSES:
        items = grouped.get(lens) or {}
        console.print(f"\n[bold]{LABELS[lens].upper()}[/bold]")

        if lens == "performance" and not result.vitals_measured:
            console.print("  [dim]Core Web Vitals were not measured. "
                          "Re-run with --vitals for LCP, CLS and blocking time.[/dim]")
        if not items:
            console.print("  [green]nothing to improve here[/green]")
            continue

        rows = sorted(items.items(),
                      key=lambda kv: (SEVERITY_RANK[kv[1][0].severity], -len(kv[1])))
        for finding_id, found in rows:
            scope = "site" if found[0].url is None else f"{len(found)}p"
            # rich knows the real terminal width; trimming by hand got it wrong.
            console.print(f"  {severity_tag[found[0].severity]}  "
                          f"{finding_id:<34.34} [dim]{scope:>5}[/dim]  {found[0].message}",
                          highlight=False, no_wrap=True, overflow="ellipsis")

    counts = Counter(f.severity for f in findings)
    console.print(
        f"\n[red]{counts[Severity.CRITICAL]} critical[/red] · "
        f"[yellow]{counts[Severity.WARNING]} warning[/yellow] · "
        f"[cyan]{counts[Severity.NOTICE]} notice[/cyan] · "
        f"[dim]{counts[Severity.INFO]} info[/dim]"
        f"   [dim]full detail in the JSON and HTML reports[/dim]\n"
    )


def _short(url: str, target: str) -> str:
    """Trim the origin off a URL so the interesting part is visible in a table."""
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    if parts.netloc and parts.netloc == urlsplit(target).netloc:
        return (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return url


def render_comparison(comparison: Comparison, minimum: Severity) -> None:
    from rich.console import Console
    from rich.table import Table
    from rich.text import Text

    console = Console(stderr=True)
    sites = comparison.sites

    console.print(f"\n[bold]Comparison[/bold] [dim]· {comparison.budget} page budget each · "
                  f"{len(comparison.rivals)} rival(s)[/dim]")

    table = Table(box=None, padding=(0, 2, 0, 0), header_style="dim")
    table.add_column("", width=30)
    for index, site in enumerate(sites):
        table.add_column(site.host or "?", justify="right",
                         style="bold" if index == 0 else "")

    scores = []
    for index, site in enumerate(sites):
        colour = "green" if site.score >= 80 else "yellow" if site.score >= 60 else "red"
        scores.append(Text(f"{site.score:.0f} ({site.grade})", style=colour))
    table.add_row(Text("Score", style="bold"), *scores)

    for row in comparison.metric_table():
        cells = [Text(cell["text"], style="green" if cell["best"] else "")
                 for cell in row["cells"]]
        table.add_row(row["label"], *cells)
    console.print()
    console.print(table)

    if gaps := comparison.category_gaps():
        console.print("\n[bold]Where a rival is ahead[/bold]")
        for gap in gaps:
            console.print(f"  [yellow]{gap.label:<14}[/yellow] {gap.detail:<14} "
                          f"[dim]{', '.join(gap.rivals)}[/dim]")

    if schema := comparison.schema_gaps():
        console.print("\n[bold]Structured data they mark up and you do not[/bold]")
        for gap in schema[:12]:
            console.print(f"  [cyan]{gap.label:<26}[/cyan] [dim]{', '.join(gap.rivals)}[/dim]")
        if len(schema) > 12:
            console.print(f"  [dim]… and {len(schema) - 12} more[/dim]")

    if tech := comparison.technology_gaps():
        console.print("\n[bold]Technology they run and you do not[/bold]")
        for gap in tech[:14]:
            console.print(f"  [dim]{gap.detail:<14}[/dim] {gap.label:<24} "
                          f"[dim]{', '.join(gap.rivals)}[/dim]")
        if len(tech) > 14:
            console.print(f"  [dim]… and {len(tech) - 14} more[/dim]")

    if strengths := comparison.strengths():
        console.print("\n[bold]Where you are ahead[/bold]")
        for gap in strengths:
            console.print(f"  [green]{gap.label:<14}[/green] {gap.detail}")

    console.print(f"\n[dim]{comparison.disclaimer}[/dim]\n")


def crawl_exit_code(result: CrawlResult, fail_on: str) -> int:
    if not result.pages:
        return 1
    if fail_on == "never":
        return 0
    threshold = SEVERITY_RANK[Severity(fail_on)]
    findings = list(result.site_findings) + [f for p in result.pages for f in p.findings]
    return 1 if any(SEVERITY_RANK[f.severity] <= threshold for f in findings) else 0


def exit_code(page: Page, fail_on: str) -> int:
    if page.error is not None or not page.ok:
        return 1
    if fail_on == "never":
        return 0
    threshold = SEVERITY_RANK[Severity(fail_on)]
    if any(SEVERITY_RANK[f.severity] <= threshold for f in page.findings):
        return 1
    return 0 if page.ok else 1


def _write(payload: str, config: CrawlConfig) -> None:
    if config.out:
        path = Path(config.out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload + "\n", encoding="utf-8")
        if not config.quiet:
            print(f"wrote {path}", file=sys.stderr)
    else:
        print(payload)


def _crawl_with_progress(config: CrawlConfig):
    """Run the crawl (and any rival crawls), showing live progress unless --quiet.

    Returns (result, scorecard, comparison-or-None).
    """
    def go(on_page=None):
        if config.against:
            return asyncio.run(run_comparison(config, on_page=on_page))
        result = asyncio.run(run_crawl(config, on_page=on_page))
        return result, scorecard(config, result), None

    if config.quiet:
        return go()

    from rich.console import Console
    from rich.progress import (
        BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn,
    )

    console = Console(stderr=True)
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(bar_width=28),
        TextColumn("{task.completed}/{task.total} pages"),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task_id = progress.add_task("crawling", total=1)

        def on_page(page: Page, frontier) -> None:
            progress.update(
                task_id,
                advance=1,
                total=max(1, frontier.accepted),
                description=f"crawling [dim]{_short(page.final_url, config.url)[:48]}[/dim]",
            )

        if config.against:
            progress.update(task_id, description=f"crawling {config.url} and "
                                                 f"{len(config.against)} rival(s)")

        return go(on_page)


def write_side_reports(config: CrawlConfig, result: CrawlResult, card: Scorecard,
                       comparison: Comparison | None = None) -> list[str]:
    """HTML, CSV and the run history. Returns lines to show the user."""
    notes: list[str] = []
    findings = all_findings(result)

    if config.html:
        context = build_context(
            target=config.url,
            pages=result.pages,
            site_findings=result.site_findings,
            score=card,
            technologies=result.technologies,
            graph=result.graph.summary() if result.graph.nodes else {},
            crawl=build_crawl_report(config, result)["crawl"],
            stats=result.stats,
            stopped_because=result.stopped_because,
            comparison=comparison,
            external_data=result.external_data,
        )
        notes.append(f"wrote {write_html(config.html, context)}")

    if config.csv:
        notes.append(f"wrote {write_csv(config.csv, findings, target=config.url)}")

    if config.db:
        with RunStore(config.db) as store:
            store.record(target=config.url, pages=len(result.pages), findings=findings,
                         score=card.overall, grade=card.grade,
                         summary=summarise(findings))
            notes.append(f"recorded run in {config.db}")
            if config.compare:
                notes.extend(_compare_lines(diff_runs(store, config.url)))
    elif config.compare:
        notes.append("--compare needs --db to have something to compare against")
    return notes


def _compare_lines(diff) -> list[str]:
    if diff is None:
        return ["no previous run for this target yet — this one is the baseline"]
    direction = "+" if diff.score_change >= 0 else ""
    lines = [f"since {diff.previous.started_at}: score {direction}{diff.score_change:.1f}, "
             f"{len(diff.introduced)} new, {len(diff.resolved)} resolved"]
    for severity, finding_id, url in diff.introduced[:5]:
        lines.append(f"  new       {severity:8} {finding_id} {url}")
    for severity, finding_id, url in diff.resolved[:5]:
        lines.append(f"  resolved  {severity:8} {finding_id} {url}")
    return lines


def main(argv: list[str] | None = None) -> int:
    config = config_from_args(argv)

    if not config.single:
        try:
            result, card, comparison = _crawl_with_progress(config)
        except KeyboardInterrupt:
            print("interrupted", file=sys.stderr)
            return 130
        _write(json.dumps(build_crawl_report(config, result, card, comparison), indent=2,
                          ensure_ascii=False), config)
        for note in write_side_reports(config, result, card, comparison):
            if not config.quiet:
                print(note, file=sys.stderr)
        if not config.quiet:
            render_crawl(result, config, Severity(config.min_severity), card)
            if comparison:
                render_comparison(comparison, Severity(config.min_severity))
        return crawl_exit_code(result, config.fail_on)

    try:
        page, seo, technologies, stats = asyncio.run(audit_single(config))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    card = single_scorecard(page)
    result = single_result(page, technologies, stats)

    _write(json.dumps(build_report(config, page, seo, technologies, stats, card),
                      indent=2, ensure_ascii=False), config)
    for note in write_side_reports(config, result, card):
        if not config.quiet:
            print(note, file=sys.stderr)

    if not config.quiet:
        render_human(page, seo, technologies, stats, Severity(config.min_severity))
        colour = "green" if card.overall >= 80 else "yellow" if card.overall >= 60 else "red"
        from rich.console import Console
        Console(stderr=True).print(
            f"[bold {colour}]Score {card.overall:.0f}/100 (grade {card.grade})"
            f"[/bold {colour}]\n")

    return exit_code(page, config.fail_on)
