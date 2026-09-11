"""Headless rendering, for pages whose content only exists after JavaScript runs.

Playwright is an optional dependency: the ~150 MB browser download is a lot to
impose on someone who only wants to check meta tags. Everything here degrades to
a clear message when it is absent.

The browser is launched once per crawl and only when something actually needs it,
because launching costs about 1.4 seconds and rendering a page costs roughly
another one.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

# Images, fonts and media change nothing about the DOM a crawler reads, and
# blocking them roughly halves render time.
BLOCKED_RESOURCES = frozenset({"image", "media", "font"})

# Resolves dotted paths (`Shopify.theme`) without eval, and never throws on a
# missing intermediate.
PROBE_SCRIPT = """(names) => names.filter(name => {
  let node = window;
  for (const part of name.split('.')) {
    if (node === null || node === undefined) return false;
    if (!(part in Object(node))) return false;
    node = node[part];
  }
  return node !== undefined;
})"""

INSTALL_HINT = (
    "Rendering needs Playwright, which is not installed. Install it with:\n"
    "    ./venv/bin/pip install playwright\n"
    "    ./venv/bin/python -m playwright install chromium"
)


# Below this, a page is worth a second look with JavaScript enabled. It matches
# the very-thin-content threshold: if the served HTML would be flagged as too
# thin to rank, it is worth knowing whether that is the whole story.
RENDER_WORD_THRESHOLD = 100

# Containers that frameworks mount into. An empty one is the clearest sign that
# the real page lives in JavaScript.
APP_SHELL_IDS = frozenset({"root", "app", "__next", "___gatsby", "__nuxt", "q-app", "main-app"})


def should_render(doc, scripts_threshold: int = 5) -> str:
    """Why this page needs rendering, or '' if the served HTML looks complete."""
    if doc is None:
        return ""

    words = doc.word_count
    if words == 0:
        return "the served HTML contains no readable text"
    if words >= RENDER_WORD_THRESHOLD:
        return ""

    for node in doc.tree.css("div[id], main[id], section[id]"):
        if (node.attributes.get("id") or "").lower() in APP_SHELL_IDS:
            return f"an app shell container with only {words} words of served text"

    if len(doc.scripts) >= scripts_threshold:
        return f"only {words} words of served text behind {len(doc.scripts)} scripts"
    return ""


def playwright_available() -> bool:
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass(slots=True)
class RenderResult:
    url: str
    html: str = ""
    globals: list[str] = field(default_factory=list)   # top-level window keys
    status: int | None = None
    elapsed_ms: float = 0.0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.html)


class Renderer:
    """Owns one browser for the whole run. Launched lazily, on first use."""

    def __init__(self, *, timeout: float = 20.0, block_resources: bool = True,
                 user_agent: str = "") -> None:
        self.timeout = timeout
        self.block_resources = block_resources
        self.user_agent = user_agent
        self._playwright: Any = None
        self._browser: Any = None
        self._launching = asyncio.Lock()
        self.rendered = 0
        self.failures = 0

    async def __aenter__(self) -> "Renderer":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close()

    async def _ensure_browser(self) -> Any:
        if self._browser is not None:
            return self._browser
        async with self._launching:
            if self._browser is not None:   # another worker got there first
                return self._browser
            from playwright.async_api import async_playwright

            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch()
        return self._browser

    async def close(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def render(self, url: str, probe_globals: list[str] | None = None) -> RenderResult:
        """Load the URL in a real browser and return the DOM once it settles.

        `probe_globals` names the JavaScript globals worth asking about. Listing
        every global instead does not work: an HTTPS page exposes some 1,200
        standard APIs, hundreds of which a blank page does not, so diffing
        against a baseline leaves mostly noise. Asking a precise question gets a
        precise answer.
        """
        import time

        if not playwright_available():
            return RenderResult(url=url, error="playwright not installed")

        started = time.perf_counter()
        context = None
        try:
            browser = await self._ensure_browser()
            context = await browser.new_context(
                user_agent=self.user_agent or None,
                ignore_https_errors=True,
            )
            page = await context.new_page()
            if self.block_resources:
                await page.route(
                    "**/*",
                    lambda route: (
                        route.abort() if route.request.resource_type in BLOCKED_RESOURCES
                        else route.continue_()
                    ),
                )

            response = await page.goto(url, wait_until="domcontentloaded",
                                       timeout=self.timeout * 1000)
            # networkidle is the signal that a client-rendered app has settled.
            # It legitimately never fires on pages with polling or open sockets,
            # so a timeout here is not a failure — take the DOM as it stands.
            try:
                await page.wait_for_load_state("networkidle", timeout=self.timeout * 1000)
            except Exception:
                pass

            html = await page.content()
            globals_seen: list[str] = []
            if probe_globals:
                globals_seen = await page.evaluate(PROBE_SCRIPT, probe_globals) or []
            self.rendered += 1
            return RenderResult(
                url=url,
                html=html,
                globals=list(globals_seen or []),
                status=response.status if response else None,
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
            )
        except Exception as exc:  # noqa: BLE001 — a failed render is data, not a crash
            self.failures += 1
            return RenderResult(
                url=url,
                error=f"{type(exc).__name__}: {exc}".split("\n")[0][:200],
                elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
            )
        finally:
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
