"""Core Web Vitals, measured in the browser we already launch for rendering.

An honest label first: these are **lab** measurements. Google ranks on *field*
data — real Chrome users on real devices and networks, at the 75th percentile —
and says plainly that "lab measurement is not a substitute for field
measurement". What a lab run gives you is a fast, repeatable signal you can act
on before the field data catches up, and a diagnosis when the field data is bad.

Two consequences of that, both deliberate:

- INP is not measured. It needs real interactions, so no lab tool can produce it.
  Total Blocking Time is the accepted stand-in and is reported as such.
- The page is loaded with throttling roughly matching Lighthouse's mobile
  profile. Without it, a page measured on a developer's machine over a fast
  connection looks far better than it is for the people actually visiting.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Google's published Core Web Vitals thresholds: good / needs improvement / poor.
THRESHOLDS: dict[str, tuple[float, float]] = {
    "lcp_ms": (2500, 4000),
    "cls": (0.1, 0.25),
    # Diagnostics, on Lighthouse's published scoring boundaries.
    "ttfb_ms": (800, 1800),
    "fcp_ms": (1800, 3000),
    "total_blocking_ms": (200, 600),
}

# Lighthouse's mobile profile: a mid-tier phone on slow 4G.
CPU_SLOWDOWN = 4
NETWORK_DOWN_BPS = 1_600_000 / 8
NETWORK_UP_BPS = 750_000 / 8
NETWORK_LATENCY_MS = 150

MOBILE_VIEWPORT = {"width": 412, "height": 823}
SETTLE_MS = 3000          # time after load for LCP and late shifts to land

# Observers must exist before navigation or the entries are simply missed.
OBSERVER_SCRIPT = """
window.__cwv = {lcp: null, cls: 0, shifts: 0, longTasks: 0, blocking: 0};
try {
  new PerformanceObserver((list) => {
    const entries = list.getEntries();
    window.__cwv.lcp = entries[entries.length - 1].startTime;
  }).observe({type: 'largest-contentful-paint', buffered: true});
} catch (e) {}
try {
  new PerformanceObserver((list) => {
    for (const entry of list.getEntries()) {
      // Shifts within 500ms of an interaction are the user's doing, not the page's.
      if (!entry.hadRecentInput) {
        window.__cwv.cls += entry.value;
        window.__cwv.shifts++;
      }
    }
  }).observe({type: 'layout-shift', buffered: true});
} catch (e) {}
try {
  new PerformanceObserver((list) => {
    for (const entry of list.getEntries()) {
      window.__cwv.longTasks++;
      window.__cwv.blocking += Math.max(0, entry.duration - 50);
    }
  }).observe({type: 'longtask', buffered: true});
} catch (e) {}
"""

COLLECT_SCRIPT = """() => {
  const nav = performance.getEntriesByType('navigation')[0] || {};
  const fcp = performance.getEntriesByName('first-contentful-paint')[0];
  const resources = performance.getEntriesByType('resource') || [];
  return {
    lcp_ms: window.__cwv.lcp,
    cls: window.__cwv.cls,
    layout_shifts: window.__cwv.shifts,
    total_blocking_ms: window.__cwv.blocking,
    long_tasks: window.__cwv.longTasks,
    fcp_ms: fcp ? fcp.startTime : null,
    ttfb_ms: nav.responseStart || null,
    load_ms: nav.loadEventEnd || null,
    requests: resources.length + 1,
    transfer_bytes: resources.reduce((t, r) => t + (r.transferSize || 0),
                                     nav.transferSize || 0),
  };
}"""


def rate(metric: str, value: float | None) -> str:
    """good / needs-improvement / poor, or 'unknown' when nothing was measured."""
    if value is None or metric not in THRESHOLDS:
        return "unknown"
    good, poor = THRESHOLDS[metric]
    if value <= good:
        return "good"
    return "needs-improvement" if value <= poor else "poor"


@dataclass(slots=True)
class Vitals:
    url: str
    lcp_ms: float | None = None
    cls: float | None = None
    fcp_ms: float | None = None
    ttfb_ms: float | None = None
    total_blocking_ms: float | None = None
    long_tasks: int = 0
    layout_shifts: int = 0
    load_ms: float | None = None
    requests: int = 0
    transfer_bytes: int = 0
    throttled: bool = True
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.lcp_ms is not None

    def ratings(self) -> dict[str, str]:
        return {metric: rate(metric, getattr(self, metric)) for metric in THRESHOLDS}

    def to_dict(self) -> dict[str, Any]:
        # asdict, not __dict__: this is a slots dataclass and has no __dict__.
        data = {k: (round(v, 1) if isinstance(v, float) else v)
                for k, v in asdict(self).items()}
        data["ratings"] = self.ratings()
        data["source"] = "lab"
        return data


async def measure(browser: Any, url: str, *, timeout: float = 45.0,
                  throttle: bool = True, user_agent: str = "") -> Vitals:
    """Load one URL under mobile conditions and collect what the browser saw."""
    import time

    context = None
    try:
        context = await browser.new_context(
            viewport=MOBILE_VIEWPORT,
            device_scale_factor=2,
            is_mobile=True,
            has_touch=True,
            user_agent=user_agent or None,
            ignore_https_errors=True,
        )
        page = await context.new_page()
        await page.add_init_script(OBSERVER_SCRIPT)

        if throttle:
            session = await context.new_cdp_session(page)
            await session.send("Network.enable")
            await session.send("Network.emulateNetworkConditions", {
                "offline": False,
                "downloadThroughput": NETWORK_DOWN_BPS,
                "uploadThroughput": NETWORK_UP_BPS,
                "latency": NETWORK_LATENCY_MS,
            })
            await session.send("Emulation.setCPUThrottlingRate", {"rate": CPU_SLOWDOWN})

        await page.goto(url, wait_until="load", timeout=timeout * 1000)
        await page.wait_for_timeout(SETTLE_MS)
        collected = await page.evaluate(COLLECT_SCRIPT)
        return Vitals(url=url, throttled=throttle, **collected)
    except Exception as exc:  # noqa: BLE001 — a failed measurement is data
        return Vitals(url=url, throttled=throttle,
                      error=f"{type(exc).__name__}: {exc}".split("\n")[0][:200])
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
