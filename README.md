# seochecker

A polite crawler that audits a website for on-page SEO and identifies the
platform, CMS and technologies behind it. CLI first; JSON out.

## Setup

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## Usage

```bash
./venv/bin/python seocheck.py example.com --single
```

The human-readable summary goes to stderr, the JSON report to stdout — so you can
pipe the report while still watching the run:

```bash
./venv/bin/python seocheck.py example.com --single | jq '.pages[0].seo'
```

Or write it to a file:

```bash
./venv/bin/python seocheck.py example.com --single -o out/report.json
```

### Useful flags

| Flag | What it does |
| --- | --- |
| `--single` | Audit one URL, don't crawl (multi-page crawling is not built yet) |
| `--delay 1.0` | Minimum seconds between requests to one host |
| `--timeout 30` | Per-request timeout |
| `--retries 3` | Retries on timeouts, connection errors and 429/5xx |
| `--user-agent browser` | Use a browser UA instead of the honest bot UA |
| `-H 'Name: value'` | Extra request header (repeatable) |
| `--cookie name=value` | Send a cookie (repeatable) — for gated sites you own |
| `--no-http2` / `--insecure` | Transport escape hatches |
| `--include-html` | Keep raw HTML in the JSON (large) |
| `--min-severity warning` | Hide findings below this severity in the terminal |
| `--fail-on critical` | Exit non-zero when a finding at this severity or above exists |
| `--rules path.yaml` | Use a custom technology rules file |
| `--min-confidence 0.8` | Hide technology detections below this confidence |

Exit code is `0` when the page fetched with a 2xx, `1` otherwise — so it can gate CI.

## What it checks

22 analyzers, each producing findings with a stable id, a severity, evidence and
a fix.

| Category | Checks |
| --- | --- |
| `title` | missing, duplicated tags, too long, too short |
| `description` | missing, duplicated, length outside the SERP window |
| `indexability` | `noindex` / `nofollow` from meta robots *and* `X-Robots-Tag`, restricted snippets |
| `canonical` | missing, conflicting, relative, cross-domain, insecure, not self-referential |
| `headings` | no headings, no `h1`, multiple `h1`, empty, skipped levels |
| `images` | **missing alt**, alt too long, alt that is just a filename or slug, missing dimensions (CLS), no lazy loading, missing `src` |
| `links` | no links, no internal links, empty anchors, generic anchor text (EN + PT), too many links, internal `nofollow`, `http://` links on an HTTPS page, unsafe `target="_blank"` |
| `social` | Open Graph missing or incomplete, relative `og:image`, `og:url` mismatch, Twitter card |
| `structured` | invalid JSON-LD, none present, microdata-only, missing required properties for 13 schema types |
| `i18n` | missing or malformed `lang`, invalid hreflang codes, no self-reference, no `x-default`, relative hreflang URLs |
| `technical` | 4xx/5xx, not HTTPS, mixed content, no HSTS, redirect chains, temporary redirects, missing viewport, undeclared charset, slow TTFB, no compression, large HTML, HTTP/1.1, no cache validators, render-blocking scripts, no favicon |
| `content` | empty (JS-rendered), very thin, thin, low text-to-HTML ratio, app-shell detection |

Severities are `critical`, `warning`, `notice`, `info`. A page that is blocked or
fails to fetch reports **only that** — a bot-mitigation challenge page is never
described as if it were the site.

## What it identifies

68 technologies across 20 categories, driven by
[`rules.yaml`](seochecker/fingerprint/rules.yaml) — adding one means adding YAML,
not code.

- **CMS / commerce** — WordPress, WooCommerce, Shopify, Wix, Squarespace, Webflow,
  Framer, Drupal, Joomla, Ghost, HubSpot, Magento, PrestaShop
- **Page builders & SEO plugins** — Elementor, Divi, Yoast SEO, Rank Math
- **Frameworks** — Next.js, Nuxt, Astro, Gatsby, SvelteKit, Hugo, Jekyll, React,
  Vue, Angular
- **Server / CDN / hosting** — nginx, Apache, LiteSpeed, IIS, Cloudflare, Vercel,
  Netlify, CloudFront, Fastly, Akamai, GitHub Pages
- **Analytics & tags** — GA4, Universal Analytics, GTM, Meta Pixel, Hotjar,
  Clarity, Plausible, Matomo, Cloudflare Web Analytics
- **Consent, chat, payments, security, libraries** — Cookiebot, OneTrust,
  CookieYes, Complianz, Intercom, Crisp, Tawk.to, Zendesk, Stripe, PayPal,
  reCAPTCHA, Turnstile, jQuery, Bootstrap, Tailwind, Font Awesome, Google Fonts

Eight signal types — response headers, cookie names, meta tags, HTML patterns,
script and stylesheet URLs, the request URL, and CSS selectors (including version
attributes like Angular's `ng-version`).

**Confidence is earned, not asserted.** Independent signals combine with a
noisy-or, so two mediocre signals that agree (0.7 + 0.7 = 0.91) beat one good one.
Implications resolve transitively — WooCommerce means WordPress means PHP — with
confidence decaying down the chain, and a strong implication can raise a weak
direct detection without overwriting its evidence.

### Writing rules

```yaml
Ghost:
  category: cms
  website: https://ghost.org
  implies: [Node.js]
  signals:
    - {type: meta, key: generator, pattern: '^Ghost(?:\s+([\d.]+))?', version: 1, confidence: 1.0}
    - {type: html, pattern: '/assets/built/', confidence: 0.5}
```

`version` is the capture group holding a version string. Use `confidence: 1.0`
only for a signal that cannot plausibly appear on a site not using the technology.

## How it fetches

- Async HTTP/2 fetch with connection reuse, brotli/zstd, honest bot UA
- Per-host request pacing with jitter; retries with exponential backoff and
  `Retry-After` support
- Redirects followed by hand so **every hop is recorded**, with loop and
  chain-length detection
- Network failures become data, not crashes: timeouts, DNS, TLS, oversized
  bodies and bot-mitigation pages all land in the report as typed errors
- Charset resolved by BOM → header → `<meta charset>` → UTF-8 → cp1252 rescue
- Basic on-page extraction: title, description, canonical, robots, lang,
  viewport, headings, word count, links (internal/external/nofollow), images
  and missing `alt`, Open Graph, JSON-LD schema types

## Tests

```bash
./venv/bin/python -m unittest discover -s tests -v
```

The suite runs against a throwaway localhost server — no outside requests.

## Layout

```
seocheck.py              entrypoint
seochecker/
  cli.py                 argument handling, report assembly, terminal output
  config.py              CrawlConfig + the CLI definition
  fetch.py               polite async fetcher (retries, redirects, decoding)
  html.py                analyzer-friendly DOM wrapper
  models.py              Page, Finding, Timing, error taxonomy
  thresholds.py          every tunable number, in one place
  fingerprint/
    rules.yaml           68 technologies, 136 signals — data, not code
    detect.py            rules engine, confidence scoring, implications
  analyzers/
    base.py              PageContext, the registry, Finding constructors
    meta.py              title, description, indexability, canonical
    headings.py images.py links.py social.py
    structured.py i18n.py content.py technical.py
tests/test_fetch.py       fetch layer: redirects, retries, failures
tests/test_analyzers.py   on-page checks, including false-positive guards
tests/test_fingerprint.py rules engine, confidence, implications
```
