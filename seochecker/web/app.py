"""The dashboard: start a crawl in a browser, watch it, read the report.

Bound to localhost by default and deliberately. This app makes outbound HTTP
requests to any address it is given, so anyone who can reach it can use the
machine as a proxy for scanning. Exposing it needs authentication in front of it,
and `--host` says so before it lets you.
"""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from typing import Any

from flask import (
    Flask, Response, abort, jsonify, redirect, render_template, request, url_for,
)

from .. import __version__
from ..cli import all_findings, build_crawl_report, summarise
from ..config import CrawlConfig, normalize_target
from ..report import RunStore, diff_runs, write_csv
from ..report.html_out import build_context, render_html
from .runner import RunManager, RunState, host_of

RENDER_CHOICES = ("auto", "never", "always")


def _int(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(request.form.get(name, default))))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(request.form.get(name, default))))
    except (TypeError, ValueError):
        return default


MAX_RIVALS = 4


def parse_rivals(raw: str) -> list[str]:
    """One rival per line or comma. Each is validated the same way the target is."""
    candidates = [part.strip() for part in re.split(r"[\n,]+", raw or "") if part.strip()]
    return [normalize_target(candidate) for candidate in candidates[:MAX_RIVALS]]


def config_from_form(form) -> CrawlConfig:
    """Build a CrawlConfig from the start form, clamping everything to sane bounds."""
    url = normalize_target(form.get("url", "").strip())
    render = form.get("render", "auto")
    return CrawlConfig(
        url=url,
        max_pages=_int("max_pages", 50, 1, 5000),
        max_depth=_int("max_depth", 5, 0, 20),
        concurrency=_int("concurrency", 4, 1, 16),
        delay=_float("delay", 0.4, 0.0, 30.0),
        include_subdomains=bool(form.get("subdomains")),
        obey_robots=not form.get("ignore_robots"),
        use_sitemap=not form.get("no_sitemap"),
        check_external=bool(form.get("check_external")),
        render=render if render in RENDER_CHOICES else "auto",
        against=parse_rivals(form.get("against", "")),
        vitals=bool(form.get("vitals")),
        # API keys are deliberately not taken from the web form: a browser form is
        # the wrong place to hand out credentials. Pass them on the command line.
        quiet=True,
    )


def create_app(*, db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    # This is a local tool people will tweak; picking up template edits without a
    # restart costs one stat per render and saves a lot of confusion.
    app.config["TEMPLATES_AUTO_RELOAD"] = True
    app.jinja_env.auto_reload = True
    app.config["RUNS"] = RunManager()
    app.config["DB_PATH"] = db_path

    def manager() -> RunManager:
        return app.config["RUNS"]

    def need(run_id: str) -> RunState:
        state = manager().get(run_id)
        if state is None:
            abort(404, "No such run. Runs are kept in memory, so restarting the "
                       "server clears them.")
        return state

    def finished(run_id: str) -> RunState:
        state = need(run_id)
        if state.result is None:
            abort(409, "That run has no result yet — it is still going, it failed, "
                       "or its result has been evicted to save memory.")
        return state

    # --- pages -------------------------------------------------------------

    @app.get("/")
    def index():
        return render_template("index.html", runs=manager().recent(),
                               version=__version__, active=manager().active)

    @app.post("/runs")
    def start_run():
        try:
            config = config_from_form(request.form)
        except ValueError as exc:
            return render_template("index.html", runs=manager().recent(),
                                   version=__version__, active=manager().active,
                                   error=str(exc)), 400
        try:
            state = manager().start(config)
        except RuntimeError as exc:
            return render_template("index.html", runs=manager().recent(),
                                   version=__version__, active=manager().active,
                                   error=str(exc)), 429
        return redirect(url_for("show_run", run_id=state.id))

    @app.get("/runs/<run_id>")
    def show_run(run_id: str):
        return render_template("run.html", run=need(run_id), version=__version__)

    @app.get("/runs/<run_id>/status")
    def run_status(run_id: str):
        return jsonify(need(run_id).to_dict())

    @app.post("/runs/<run_id>/cancel")
    def cancel_run(run_id: str):
        return jsonify({"cancelled": manager().cancel(run_id)})

    @app.get("/runs/<run_id>/report")
    def run_report(run_id: str):
        return Response(_report_html(finished(run_id)), mimetype="text/html")

    @app.get("/runs/<run_id>/download/<fmt>")
    def download(run_id: str, fmt: str):
        state = finished(run_id)
        host = re.sub(r"[^A-Za-z0-9.-]", "_", host_of(state.target))

        if fmt == "html":
            return _attachment(_report_html(state), f"{host}-report.html", "text/html")
        if fmt == "json":
            payload = json.dumps(
                build_crawl_report(state.config, state.result, state.card, state.comparison),
                indent=2, ensure_ascii=False)
            return _attachment(payload, f"{host}-report.json", "application/json")
        if fmt == "csv":
            buffer = io.StringIO()
            import csv as csv_module
            from ..report.csv_out import COLUMNS
            writer = csv_module.DictWriter(buffer, fieldnames=COLUMNS)
            writer.writeheader()
            for finding in all_findings(state.result):
                writer.writerow({
                    "severity": finding.severity.value, "category": finding.category,
                    "finding_id": finding.id, "url": finding.url or state.target,
                    "message": finding.message, "evidence": finding.evidence,
                    "fix": finding.fix,
                })
            return _attachment(buffer.getvalue(), f"{host}-findings.csv", "text/csv")
        abort(404, "Unknown format. Use html, json or csv.")

    @app.get("/history")
    def history():
        db = app.config["DB_PATH"]
        if not db or not Path(db).exists():
            return render_template("history.html", version=__version__, rows=[], db=db)
        with RunStore(db) as store:
            targets = {row["target"] for row in store.connection.execute(
                "SELECT DISTINCT target FROM runs").fetchall()}
            rows = []
            for target in sorted(targets):
                runs = store.runs_for(target, limit=10)
                rows.append({"target": target, "runs": runs,
                             "diff": diff_runs(store, target)})
        return render_template("history.html", version=__version__, rows=rows, db=db)

    # --- helpers -----------------------------------------------------------

    def _report_html(state: RunState) -> str:
        result = state.result
        return render_html(build_context(
            target=state.target,
            pages=result.pages,
            site_findings=result.site_findings,
            score=state.card,
            technologies=result.technologies,
            graph=result.graph.summary() if result.graph.nodes else {},
            crawl=build_crawl_report(state.config, result)["crawl"],
            stats=result.stats,
            stopped_because=result.stopped_because,
            comparison=state.comparison,
        ))

    def _attachment(body: str, filename: str, mimetype: str) -> Response:
        return Response(body, mimetype=mimetype, headers={
            "Content-Disposition": f'attachment; filename="{filename}"'})

    @app.errorhandler(404)
    @app.errorhandler(409)
    @app.errorhandler(429)
    def handle_error(exc):
        code = getattr(exc, "code", 500)
        return render_template("error.html", version=__version__, code=code,
                               message=getattr(exc, "description", str(exc))), code

    return app
