# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Python CLI that warms the Cloudflare edge cache. It walks each configured site's sitemap and fetches every URL with browser-like headers. It runs hourly from GitHub Actions (`.github/workflows/preload.yml`) against the sites in `config/sites.yml`: the IEM and UEM WordPress sites, which both run Wordfence. It writes a per-site `cf-cache-status` breakdown to `$GITHUB_STEP_SUMMARY`.

## Commands

Uses `uv` (Python 3.11, from `.python-version`; deps locked in `uv.lock`).

```bash
uv sync --extra dev                                                   # install (add --extra playwright for the browser fetcher)
uv run pytest -q                                                      # all tests
uv run pytest tests/test_sitemap.py::test_name -q                     # single test
uv run ruff check src tests && uv run ruff format --check src tests   # lint, same as CI
uv run python -m preloader --config config/sites.yml --dry-run        # discover URLs only, no page fetches
uv run python -m preloader --config config/sites.yml --site "IEM Website" --max-urls 20 -v
```

CI (`ci.yml`) runs pytest, `ruff check` and `ruff format --check` with `--locked`, so `uv.lock` must be kept in sync with `pyproject.toml`. Ruff uses line length 120 and excludes `.claude/` (vendored skills).

A run without `--dry-run` sends real traffic to production sites. Use `--dry-run`, or `--site` plus `--max-urls`, when checking a change locally.

## Architecture

Pipeline: `cli.py` → `config.load_config().resolve()` → `runner.run()` → `summary.write()`.

- **Config (`config.py`)**: `Defaults` and `Site` are pydantic models with `extra="forbid"`, so a misspelled YAML key is a validation error. `Config.resolve()` merges the defaults into each `Site` and produces a `ResolvedSite`, which is the only config type the runner and fetchers see. A new config knob needs four changes: a field on `Defaults`, a field on `Site`, a field on `ResolvedSite`, and a line in `resolve()`. Also document it in `config/sites.example.yml`. Header values support `${VAR}` env interpolation (braced form only). An unset variable raises an error.
- **Discovery (`sitemap.py`)**: walks `<sitemapindex>` recursively and sequentially, with a depth cap of 3, a 50 MB uncompressed size cap, gzip support, and defusedxml for parsing. `sitemap_urls` is an ordered fallback list: the first entry that yields URLs wins. Errors are reported only if no entry yields URLs and none parsed cleanly; a valid but empty sitemap is not an error.
- **Fetching (`fetcher.py`)**: `HttpxFetcher` (HTTP/2) and `PlaywrightFetcher`. Playwright is lazy-imported, blocks images, media, fonts and stylesheets, and CI installs it only when some site sets `fetcher: playwright`. Both fetchers share `_fetch_with_retry`, which never raises (failures come back as a `FetchResult` with `error` set). It retries on 429/502/503/504 and on transient transport errors, and honours `Retry-After` up to 60 s. Tests stub out backoff by monkeypatching `fetcher._sleep`.
- **Rate limiting**: `RateLimitedTransport` wraps the httpx transport, so `request_delay_ms` applies to every outbound HTTP request, including redirect follow-ups and sitemap fetches. It is not applied per logical URL. Playwright uses the same `_MinGap` timer directly. The `concurrency` semaphore in `runner.py` only caps how many URLs are in flight. It does not control request rate.
- **Runner (`runner.py`)**: sites run in parallel via `asyncio.gather`. Each site's run is wrapped so that one site crashing produces an error report instead of killing the others. `time_budget_seconds` is a single deadline covering both discovery and fetching. When it expires, pending fetches are cancelled and counted in `budget_skipped`, and the partial report is still produced.
- **Exit codes (`cli.py`)**: 2 for a bad config or an unknown `--site`. 1 if any site crashed, if any site discovered nothing because its sitemaps failed, or (outside a dry run) if any site that discovered URLs got zero successful fetches.

## Operational constraints

The comments in `config/sites.yml` explain these numbers. Read them before changing tuning values.

- Wordfence blocks IPs that exceed about 240 requests/min. Keep `request_delay_ms` at 500 ms or more for these sites, which gives about 120 req/min.
- Throughput is limited by whichever is slower: the request gap, or concurrency × origin response time on cache MISSes. WordPress takes about 2–3.5 s per MISS.
- `time_budget_seconds` (2400) must stay below the workflow's `timeout-minutes: 45`, so that the summary is still written before GitHub kills the job.
- GitHub-hosted runners egress from the US, so these runs warm US Cloudflare POPs, not Indian or SEA ones.
