# AGENTS.md

## Commands
- `uv sync --extra dev` — install deps + test/dev tools
- `uv sync --extra playwright` — add Playwright if any site uses `fetcher: playwright`
- `uv run pytest -q` — run all tests
- `uv run pytest tests/test_sitemap.py::test_name -q` — run a single test
- `uv run ruff check src tests && uv run ruff format --check src tests` — lint/format (CI runs with `--locked`)
- `uv run python -m preloader --config config/sites.yml --dry-run` — discover URLs only (no fetches)
- `uv run python -m preloader --config config/sites.yml --site "IEM Website" --max-urls 20 -v` — run one site locally

CI (`.github/workflows/ci.yml`) requires `uv.lock` to be in sync with `pyproject.toml` (uses `--locked`). Ruff line length 120, excludes `.claude/`.

## Architecture
- **Pipeline**: `cli.py` → `config.load_config().resolve()` → `runner.run()` → `summary.write()`. `ResolvedSite` is the only config type runner/fetchers see.
- **Config (`config.py`)**: `Defaults` and `Site` use `pydantic` with `extra="forbid"` — misspelled YAML keys are validation errors. Adding a new config knob requires 4 changes: add to `Defaults`, add to `Site`, add to `ResolvedSite`, wire in `Config.resolve()`; also document in `config/sites.example.yml`. Header values only interpolate braced `${VAR}` (literal `$` untouched); unset var raises. Site `max_urls` overrides defaults `max_urls_per_site`. Filters are stored as compiled `re.Pattern`s.
- **Sitemap (`sitemap.py`)**: tries `sitemap_urls` in order; the **first entry that yields any URLs wins**. Earlier failures are logged but don't block fallback. Errors are reported only if no entry yields URLs and none parsed cleanly (a valid but empty sitemap is not an error). Recursive `sitemapindex` up to depth 3, 50 MB uncompressed cap, gzip supported (including concatenated members), parsed with `defusedxml`. URLs deduped and filtered by regex.
- **Fetching (`fetcher.py`)**: `HttpxFetcher` (httpx[http2]) and lazy-imported `PlaywrightFetcher`. Playwright blocks `image`, `media`, `font`, `stylesheet` (scripts allowed); startup failures return a `FetchResult` with error. Both share `_fetch_with_retry` which **never raises** — any per-URL failure becomes a `FetchResult` with `error` set. Retries on 429/502/503/504 and transient transport errors; honours `Retry-After` up to 60 s (capped at 60). Tests stub backoff by monkeypatching `fetcher._sleep`.
- **Rate limiting**: `RateLimitedTransport` wraps httpx transport so `request_delay_ms` applies to every outbound HTTP request (including redirects and sitemap fetches). Playwright uses `_MinGap` directly. `runner`'s `asyncio.Semaphore(concurrency)` only caps in-flight URLs — it does not control request rate.
- **Runner (`runner.py`)**: sites run in parallel via `asyncio.gather`; each site is wrapped so one crashing site produces an error report without killing others. `time_budget_seconds` is a single deadline covering discovery + fetching. On expiry, pending fetches are cancelled, `budget_skipped`/`budget_exhausted` set, and a partial report is still written. Discovery client also uses `RateLimitedTransport` with the site's request delay.
- **Summary (`summary.py`)**: writes a markdown table of `cf-cache-status` breakdown (HIT/MISS/EXPIRED/REVALIDATED/DYNAMIC/BYPASS/Other), HIT%, errors, duration to `$GITHUB_STEP_SUMMARY` when set. Lists time-budget and sitemap warnings, and collapses fetch errors (first 50 shown).
- **Exit codes (`cli.py`)**: `2` = bad config or unknown `--site`. `1` = any site crashed OR any site discovered 0 because sitemaps failed OR (outside `--dry-run`) any site that discovered URLs got 0 successful fetches.

## Operational gotchas
- **Wordfence**: blocks >~240 req/min. Keep `request_delay_ms >= 500ms` (~120 req/min) for the configured WordPress sites. Current: defaults 250ms (caution), per-site 500ms. Throughput = min(request gap, `concurrency × 60/resp_time`) — WordPress MISSes can be 2–3.5s, so tune both together.
- **Timeouts**: `time_budget_seconds` (defaults 2400s in config) must stay below the workflow's `timeout-minutes: 45` (`.github/workflows/preload.yml`) so the summary still writes before GitHub kills the job.
- **Region**: GitHub-hosted runners egress from US → warm US Cloudflare POPs, not India/SEA. To warm Indian/SEA POPs, use a self-hosted runner in Mumbai/Singapore (see README).
- **Playwright opt-in**: CI checks if any site has `fetcher: playwright` and only then installs it (`uv sync --extra playwright && playwright install --with-deps chromium`). `curl-impersonate` is deprecated (Cloudflare detects TLS fingerprint) — don't use it.
- **Safety**: running without `--dry-run` sends real traffic to production sites. Prefer `--dry-run`, `--site`, or `--max-urls` for local checks. Sites run in parallel but each site's rate limiting is independent via its own transport/gap.