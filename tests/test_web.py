"""Phase 10 acceptance tests: the dashboard.

Crawls run in a background thread, so most of these drive the HTTP surface and
poll, exactly as the browser does.
"""

from __future__ import annotations

import csv
import io
import json
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from seochecker.web import create_app
from seochecker.web.app import config_from_form
from seochecker.web.runner import MAX_CONCURRENT

PROSE = ("A oficina produz sapatos artesanais em Guimaraes desde mil novecentos e oitenta e "
         "sete, com couro curtido a taninos vegetais e solas cosidas a mao. ")


def doc(title: str, links: str = "") -> bytes:
    return (f'<!doctype html><html lang="pt"><head><meta charset="utf-8">'
            f'<title>{title} na oficina de sapatos artesanais</title>'
            f'<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
            f"<body><h1>{title}</h1><p>{PROSE * 5}</p>{links}</body></html>").encode()


class Site(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):  # noqa: N802
        if self.path in ("/robots.txt", "/sitemap.xml"):
            body, status, ctype = b"", 404, "text/plain"
        elif self.path == "/":
            links = " ".join(f'<a href="/p{i}">p{i}</a>' for i in range(12))
            body, status, ctype = doc("Inicio", links + '<a href="/sobre">sobre</a>'), 200, "text/html"
        elif self.path == "/sobre" or self.path.startswith("/p"):
            body, status, ctype = doc("Pagina", '<a href="/">inicio</a>'), 200, "text/html"
        else:
            body, status, ctype = doc("Nao encontrado"), 404, "text/html"
        self.send_response(status)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass


class FormTests(unittest.TestCase):
    """Anything the form accepts reaches a live crawler, so bounds matter."""

    class FakeForm(dict):
        def get(self, key, default=None):
            return dict.get(self, key, default)

    def build(self, **fields):
        form = self.FakeForm({"url": "example.com", **fields})
        import seochecker.web.app as web_app

        class Request:
            pass

        original = web_app.request
        web_app.request = Request()
        web_app.request.form = form
        try:
            return config_from_form(form)
        finally:
            web_app.request = original

    def test_defaults_are_sane(self):
        config = self.build()
        self.assertEqual(config.url, "https://example.com/")
        self.assertTrue(config.obey_robots)
        self.assertEqual(config.render, "auto")

    def test_absurd_values_are_clamped(self):
        config = self.build(max_pages="999999", concurrency="500", delay="-4")
        self.assertLessEqual(config.max_pages, 5000)
        self.assertLessEqual(config.concurrency, 16)
        self.assertGreaterEqual(config.delay, 0.0)

    def test_junk_values_fall_back_to_defaults(self):
        config = self.build(max_pages="banana", delay="", concurrency=None)
        self.assertEqual(config.max_pages, 50)
        self.assertEqual(config.delay, 0.4)

    def test_unknown_render_mode_is_not_passed_through(self):
        self.assertEqual(self.build(render="rm -rf").render, "auto")

    def test_checkboxes(self):
        config = self.build(ignore_robots="on", subdomains="on", check_external="on")
        self.assertFalse(config.obey_robots)
        self.assertTrue(config.include_subdomains)
        self.assertTrue(config.check_external)

    def test_an_unusable_url_is_rejected(self):
        for bad in ("ftp://example.com", "not a url at all", "https://exa mple.com", ""):
            with self.subTest(url=bad), self.assertRaises(ValueError):
                self.build(url=bad)


class DashboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = QuietServer(("127.0.0.1", 0), Site)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}/"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()

    def start(self, **overrides) -> str:
        form = {"url": self.base, "max_pages": "4", "delay": "0", "concurrency": "2",
                "render": "never", "no_sitemap": "on"}
        form.update(overrides)
        response = self.client.post("/runs", data=form)
        self.assertEqual(response.status_code, 302)
        return response.headers["Location"].rsplit("/", 1)[1]

    def wait_for(self, run_id: str, timeout: float = 30.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self.client.get(f"/runs/{run_id}/status").get_json()
            if state["status"] != "running":
                return state
            time.sleep(0.1)
        self.fail("crawl did not finish in time")

    # --- pages -------------------------------------------------------------

    def test_the_form_renders(self):
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn("Start crawl", body)
        self.assertIn("Page budget", body)

    def test_history_without_a_database_explains_itself(self):
        body = self.client.get("/history").get_data(as_text=True)
        self.assertIn("No database configured", body)

    def test_a_bad_url_is_rejected_with_a_message(self):
        response = self.client.post("/runs", data={"url": "not a url at all"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("not a valid hostname", response.get_data(as_text=True).lower())

    def test_unknown_run(self):
        self.assertEqual(self.client.get("/runs/nope").status_code, 404)
        self.assertEqual(self.client.get("/runs/nope/status").status_code, 404)

    # --- a run, end to end -------------------------------------------------

    def test_a_crawl_runs_and_reports(self):
        run_id = self.start()
        state = self.wait_for(run_id)
        self.assertEqual(state["status"], "done")
        self.assertGreaterEqual(state["pages_done"], 2)
        self.assertIsNotNone(state["score"])
        self.assertIn(state["grade"], list("ABCDEF"))

    def test_the_report_is_only_offered_once_there_is_one(self):
        app = create_app()
        client = app.test_client()
        response = client.post("/runs", data={"url": self.base, "max_pages": "4",
                                              "delay": "2", "concurrency": "1",
                                              "render": "never", "no_sitemap": "on"})
        run_id = response.headers["Location"].rsplit("/", 1)[1]
        self.assertEqual(client.get(f"/runs/{run_id}/report").status_code, 409)
        client.post(f"/runs/{run_id}/cancel")

    def test_downloads(self):
        run_id = self.start()
        self.wait_for(run_id)

        html = self.client.get(f"/runs/{run_id}/download/html")
        self.assertEqual(html.status_code, 200)
        self.assertIn("attachment", html.headers["Content-Disposition"])
        self.assertIn("SEO report", html.get_data(as_text=True))

        payload = self.client.get(f"/runs/{run_id}/download/json").get_json()
        self.assertEqual(payload["mode"], "crawl")
        self.assertIn("score", payload)

        rows = list(csv.DictReader(io.StringIO(
            self.client.get(f"/runs/{run_id}/download/csv").get_data(as_text=True))))
        self.assertTrue(rows)
        self.assertIn("finding_id", rows[0])

        self.assertEqual(self.client.get(f"/runs/{run_id}/download/pdf").status_code, 404)

    def test_a_crawl_can_be_stopped_and_keeps_what_it_found(self):
        run_id = self.start(delay="2", concurrency="1", max_pages="20")
        # Wait for it to be demonstrably running rather than guessing at a sleep.
        for _ in range(50):
            if self.client.get(f"/runs/{run_id}/status").get_json()["pages_done"] >= 1:
                break
            time.sleep(0.1)
        self.assertTrue(self.client.post(f"/runs/{run_id}/cancel").get_json()["cancelled"])
        state = self.wait_for(run_id)
        self.assertEqual(state["status"], "cancelled")
        self.assertEqual(self.client.get(f"/runs/{run_id}/report").status_code, 200)

    def test_only_so_many_crawls_at_once(self):
        ids = [self.start(delay="3", concurrency="1", max_pages="20")
               for _ in range(MAX_CONCURRENT)]
        refused = self.client.post("/runs", data={"url": self.base, "delay": "3"})
        self.assertEqual(refused.status_code, 429)
        for run_id in ids:
            self.client.post(f"/runs/{run_id}/cancel")


if __name__ == "__main__":
    unittest.main(verbosity=2)
