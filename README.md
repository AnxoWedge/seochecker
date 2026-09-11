# seochecker

A polite crawler that audits a website for on-page SEO and identifies the
platform, CMS and technologies behind it. CLI first; JSON out.

## Setup

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## Dashboard

```bash
./venv/bin/pip install flask
./venv/bin/python serve.py --db runs.sqlite3
```

Then open <http://127.0.0.1:8770>. Paste a URL, watch the crawl page by page, and
read the report in the browser — or download it as HTML, JSON or CSV. Crawls can
be stopped mid-run and still produce a report for what they found. The History tab
shows every run recorded to the database, with the score change and which findings
appeared or were fixed since last time.

It binds to localhost, and that default matters: this app fetches any URL it is
given, so anything that can reach it can use the machine to make requests on its
behalf. `--host` prints a warning before it lets you change that; put
authentication in front of it first.

## Usage

Crawl a site and produce a report you can send to someone:

```bash
./venv/bin/python seocheck.py example.com --html out/report.html -o out/report.json
```

Compare against rivals:

```bash
./venv/bin/python seocheck.py mysite.com --against rival-a.com --against rival-b.com \
  --max-pages 50 --html report.html
```

Or audit a single page:

```bash
./venv/bin/python seocheck.py example.com/pricing --single
```

The human-readable summary goes to stderr, the JSON report to stdout — so you can
pipe the report while still watching the run:

```bash
./venv/bin/python seocheck.py example.com | jq '.summary.by_id'
```

### Useful flags

| Flag | What it does |
| --- | --- |
| `--single` | Audit one URL instead of crawling |
| `--against rival.com` | Also crawl this rival and compare against it (repeatable) |
| `--max-pages 200` | Page budget for the crawl (default 500) |
| `--max-depth 3` | How many links deep to follow (default 5) |
| `--max-time 600` | Stop after this many seconds |
| `-c 8` | Concurrent requests (default 5) |
| `--subdomains` | Follow links into subdomains too |
| `--include` / `--exclude` | Regex filters on URLs (repeatable) |
| `--no-sitemap` | Don't seed the crawl from sitemaps |
| `--ignore-robots` | Ignore robots.txt — only for sites you own |
| `--check-external` | Also verify external links resolve (requests to third-party servers) |
| `--no-soft-404-probe` | Skip the single request that tests how the site handles a missing URL |
| `--render never\|auto\|always` | Headless rendering policy (default `auto`) |
| `--max-render 50` | Cap on pages rendered (default 25) |
| `--delay 1.0` | Minimum seconds between requests to one host |
| `--timeout 30` | Per-request timeout |
| `--retries 3` | Retries on timeouts, connection errors and 429/5xx |
| `--cache PATH` | On-disk response cache — makes re-running an audit nearly free for the site |
| `--cache-ttl 3600` | How long a cached response is used before revalidating |
| `--max-blocks 5` | Stop asking a host after this many consecutive blocks |
| `--user-agent browser` | Use a browser UA instead of the honest bot UA |
| `-H 'Name: value'` | Extra request header (repeatable) |
| `--cookie name=value` | Send a cookie (repeatable) — for gated sites you own |
| `--no-http2` / `--insecure` | Transport escape hatches |
| `--include-html` | Keep raw HTML in the JSON (large) |
| `--min-severity warning` | Hide findings below this severity in the terminal |
| `--fail-on critical` | Exit non-zero when a finding at this severity or above exists |
| `--rules path.yaml` | Use a custom technology rules file |
| `--min-confidence 0.8` | Hide technology detections below this confidence |

Exit code is `0` on success, `1` when the fetch failed or `--fail-on` is triggered —
so it can gate CI.

## Reports

`--html` writes a single self-contained file: no CDN, no web fonts, no network.
It opens from `file://`, from an email attachment, and on a laptop with no
internet, which is how it will actually be read. Light and dark, prints cleanly,
and the tables sort and filter in place.

It contains the score and its breakdown, counts by severity, the technology
stack, crawl and link-graph summaries, every site-wide finding, page findings
grouped by issue rather than repeated per page, and a sortable table of every URL
with its status, click depth, inbound links and internal PageRank.

`--db` records each run in SQLite and `--compare` diffs the last two — what broke,
what got fixed, and how the score moved.

### The score

A heuristic, and built to be argued with: each area starts at 100 and every
distinct finding deducts according to its severity and the **share of the site it
affects**, so a critical issue on one page in a hundred barely registers while the
same issue everywhere is devastating. The report shows the deduction behind every
number.

The first version of this model was wrong in a way worth recording: it averaged
the per-area scores, so a site with every page set to `noindex` — completely
invisible to search — scored **94.8 out of 100**, because seventeen clean areas
drowned the one catastrophic one. The overall is now computed from the findings
directly, with weights fitted against a table of stated expectations:

| Site | Score |
| --- | --- |
| No findings | 100 |
| Every page `noindex` | 0 |
| One critical on 1 page in 100 | 99 |
| One critical on every page | 10 |
| One warning on every page | 85 |
| All internal links broken | 33 |

For reference, measured: wordpress.org scores 72, mozilla.org 70, and a
well-built single page (MDN) 86.

## JavaScript rendering

Optional, and off unless needed:

```bash
./venv/bin/pip install -r requirements-render.txt
./venv/bin/python -m playwright install chromium
```

Without it, everything else still works — `--render` just reports that Playwright
is missing.

`--render auto` (the default) renders only pages whose served HTML looks
incomplete: no readable text at all, or fewer than 100 words combined with an
empty app-shell container (`#root`, `#app`, `#__next`…) or five or more scripts.
Verified against wordpress.org, mozilla.org and example.com, it renders **zero**
pages — a normal server-rendered site never pays for a browser launch.

When a page is rendered, the crawler:

- analyses the **rendered** DOM, so the audit reflects what a rendering crawler sees
- follows links that only exist after JavaScript — an SPA that is a dead end
  without rendering becomes a crawlable site with it
- records the difference, and reports content, links, titles or descriptions that
  exist *only* after rendering. Google renders JavaScript, but on a delay and a
  budget; Bing, social preview bots and most AI crawlers largely do not.
- probes for the JavaScript globals the fingerprint rules ask about (`Shopify`,
  `wp`, `__NEXT_DATA__`, …), which catches platforms that leave no trace in the
  served HTML

Rendering costs roughly 1-10 seconds per page, hence `--max-render`.

## Comparing against rivals

`--against` crawls each rival to the **same page budget** as the target — a
comparison of 200 pages against 12 is not a comparison — under the same politeness
rules, with their robots.txt respected exactly like yours. Rival crawls run
concurrently because they are different hosts, so no single site is asked for more
than it would be in a solo crawl.

The report gains a side-by-side table (content volume, thin-page share, structured
data coverage, internal linking, click depth, HTTPS, compression, HTTP version,
server response time, HTML weight), plus three lists:

- **Where a rival is ahead** — category scores they beat you on by a meaningful margin
- **Structured data they mark up and you do not** — usually the most actionable gap,
  since schema markup is a direct request for rich results and costs only the markup
- **Technology they run and you do not** — CDN, analytics, consent, page builders

And **where you are ahead**, which only counts when you beat every rival.

### What this cannot tell you

A crawl sees what is on the pages: markup, structure, technology, delivery. It
cannot see backlinks, traffic, or rankings. So this answers "what are they doing on
their pages that we are not" and never "who ranks better". That sentence is printed
in the report itself, because a comparison like this is easy to over-read.

Real example — `wordpress.org` against `ghost.org`, ten pages each:

| | wordpress.org | ghost.org |
| --- | ---: | ---: |
| Score | 75 (C) | **86 (B)** |
| Median words per page | 482 | **889** |
| Pages with structured data | 20% | **100%** |
| Median server response | 364ms | **65ms** |

## Not getting blocked

The aim is to behave so a site never needs to block us — not to defeat bot
protection. There is no CAPTCHA solving and no WAF evasion here. When a site does
block us, that is reported as a finding rather than worked around.

- **Pacing adapts to the host.** A fixed delay is either too slow for a healthy
  server or too fast for a struggling one. Each host starts at the configured
  delay and adjusts: hard backoff on 429/503, gentler on errors and on rising
  latency, and a deliberately slow return toward the base once it is answering
  normally. `Retry-After` is honoured, and a `Crawl-delay` in robots.txt can only
  ever slow the crawl down.
- **A circuit breaker stops the crawl.** After five consecutive blocks from a
  host, seochecker stops asking and reports why. Measured against a server that
  rate-limits everything: with 41 discoverable URLs, it sent **5 requests** and
  refused the rest locally.
- **The cache is a politeness feature.** With `--cache`, a fresh entry is served
  with no request at all and a stale one is revalidated with `If-None-Match`,
  which the server answers with a bodiless 304. Measured on a 17-page site: a
  second run took **1 request instead of 20**, and forcing revalidation turned 18
  of 20 into 304s.
- **Interrupted crawls restart cheaply.** There is no separate resume checkpoint,
  deliberately — with the cache on, re-running a crawl that stopped at page 400
  replays those from disk and only fetches what is new.

For sites you own, `-H 'Name: value'` and `--cookie name=value` get past a login,
`--proxy` routes elsewhere, and `--ignore-robots` exists but is off by default.

## How it crawls

- **robots.txt** is parsed per RFC 9309: per-agent groups, longest-match
  precedence, `Allow` winning ties, `*` and `$` wildcards, plus `Crawl-delay` and
  `Sitemap:` discovery. A declared crawl delay can only ever slow the crawl down,
  never speed it up.
- **Sitemaps** are found from robots.txt (falling back to `/sitemap.xml`),
  including `<sitemapindex>` recursion, gzipped `.xml.gz` files and the plain-text
  form. Entries are then cross-checked against the crawl: sitemap URLs that 404,
  redirect, are `noindex`, or canonicalise elsewhere all get reported.
- **URL normalization** decides what counts as the same page: lowercased host,
  no fragment, no default port, tracking parameters dropped (`utm_*`, `fbclid`,
  `gclid` and friends), remaining parameters sorted. Path case and trailing
  slashes are *preserved*, because servers are allowed to treat them as
  significant and plenty do.
- **Scope** defaults to the target's own site, treating `www.` and the bare host
  as one. `--subdomains` widens it with a dot-anchored suffix match, so
  `evil-example.com` cannot pass as `example.com`.
- **The budget is shared.** A quarter of `--max-pages` is seeded from the sitemap
  up front and the rest is left for link discovery, with a top-up pass from the
  sitemap once links run out. Seeding the whole sitemap first would mean the link
  graph is never seen; seeding none of it would mean sitemap entries are never
  validated.
- **Every skipped URL is counted with a reason** — off-site, already seen, beyond
  max depth, blocked by robots.txt — so a crawl that visits 12 pages when you
  expected 500 explains itself.

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
| `url` | too long, too deep, uppercase, underscores, session IDs, too many parameters |

## What it finds across the whole site

Checks that need the finished crawl rather than one page:

| Category | Checks |
| --- | --- |
| `duplicate` | identical or near-identical titles, meta descriptions, H1s and body content |
| `structure` | orphan pages, pages unreachable by following links, pages more than 3 clicks deep, dead ends, broken internal links *with the pages that contain them*, links pointing at redirects, soft 404s |
| `sitemap` | missing, partly unreadable, and entries that 404, redirect, are `noindex` or canonicalise elsewhere |
| `robots` | missing, unreachable, blocking the entire site, no sitemap declared, **blocking a named search engine**, **blocking the CSS/JS pages need**, AI crawlers blocked (recorded, not judged) |
| `crawl` | parameter explosion from faceted navigation, URLs blocked by robots.txt |
| `external` | broken outbound links (with `--check-external`) |

### Internal PageRank

The report includes a PageRank computed over the site's own internal link graph:
where authority actually pools, which pages are orphaned, and how many clicks
each page is from the homepage. No third-party API is involved, and it answers
the question people usually mean when they ask about authority.

### Near-duplicate detection

Pages are compared with a bottom-k MinHash sketch, which estimates what
proportion of their content two pages share — so a finding reads "these two pages
are 80% identical" rather than an opaque distance.

The 60% threshold is calibrated, not guessed. Measured across a crawl of
wordpress.org, genuinely unrelated pages on the same site sit at 10% median
similarity and 16% at the 90th percentile, while its one real near-duplicate pair
— two nearly identical privacy request forms — measures 62%. Tune it via
`Thresholds.near_duplicate_similarity`.

Pages that declare themselves duplicates, by `noindex` or by canonicalising
elsewhere, are excluded. That is the fix, not the fault.

### Language versions

Google's rule is exact: *"Localized versions of a page are only considered
duplicates if the main content of the page remains untranslated."* seochecker
follows it.

Pages that declare each other as `hreflang` alternates are treated as one page in
several languages, so a shared title, description or H1 between them is not
reported — a brand name does not get translated. What *is* reported is the case
Google actually calls a duplicate: alternates serving identical untranslated body
content (`duplicate.untranslated_localizations`), and alternates that are nearly
identical, which usually means a translation that was never finished
(`duplicate.partly_translated`).

Reciprocity is required before two pages count as alternates, because Google
ignores one-way `hreflang`: *"If two pages don't both point to each other, the
tags will be ignored."* An alternate the crawl budget never reached is given the
benefit of the doubt.

Severities are `critical`, `warning`, `notice`, `info`. A page that is blocked or
fails to fetch reports **only that** — a bot-mitigation challenge page is never
described as if it were the site.

A page marked `noindex` is deliberately kept out of search, so it is not judged on
how it would look there: title length, meta description, canonical, social tags,
structured data and thin content are all suppressed for it. What it *is* still
judged on is everything that does not stop mattering — image alt text, headings,
charset, viewport, security, and whether a crawler following `noindex, follow` can
get out of it.

Named crawlers are checked by token, so `Applebot`, `Qwantbot`, `Bingbot`,
`DuckDuckBot` and the rest are each verified against robots.txt rather than assumed
to behave like Googlebot.

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
seocheck.py              CLI entrypoint
serve.py                 dashboard entrypoint
seochecker/
  cli.py                 argument handling, report assembly, terminal output
  config.py              CrawlConfig + the CLI definition
  fetch.py               polite async fetcher (retries, redirects, decoding)
  html.py                analyzer-friendly DOM wrapper
  models.py              Page, Finding, Timing, error taxonomy
  thresholds.py          every tunable number, in one place
  crawl.py               orchestrator: robots -> sitemap -> frontier -> fetch
  frontier.py            the queue, dedup, and why URLs were skipped
  urls.py                normalization and scope rules
  robots.py              robots.txt parser (RFC 9309)
  sitemap.py             sitemap, sitemapindex, gzip and plain-text forms
  graph.py               internal link graph: PageRank, click depth, orphans
  similarity.py          MinHash sketches for near-duplicate detection
  render.py              headless rendering and the decision of when to use it
  web/
    app.py runner.py     the dashboard: routes, and crawls on background threads
    templates/           its pages
  score.py               the scoring model, and the calibration behind it
  compare.py             rival comparison: metrics and gap analysis
  language.py            hreflang clusters, so translations are not duplicates
  report/
    html_out.py          self-contained HTML report
    template.html.j2     its markup, styles and interactions
    csv_out.py store.py  CSV export and the SQLite run history
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
tests/test_crawl.py       scope, dedup, robots, sitemap, limits
tests/test_sitewide.py    duplicates, link graph, soft 404s, external links
tests/test_render.py      the render heuristic, and rendering end to end
tests/test_report.py      the scoring calibration table, and the report writers
tests/test_politeness.py  pacing, the circuit breaker, and the cache
tests/test_web.py         the dashboard, end to end
tests/test_compare.py     comparison metrics, gaps, and the equal-budget contract
tests/test_language.py    language-aware duplicate detection
```
