# Cloudflare Cache Preloader

Pre-warms the Cloudflare edge cache by walking each site's sitemap and fetching every URL with browser-like headers, on a schedule, from GitHub Actions. Publishes a per-site summary (URLs scanned, `cf-cache-status` breakdown, errors) to the GitHub Actions run summary.

## How it works

1. Reads `config/sites.yml`.
2. For each site, fetches the configured `sitemap_urls` and recursively walks any `<sitemapindex>` (gzipped sitemaps supported).
3. Fetches every discovered URL with the configured `User-Agent`, `Accept`, etc., bounded by `concurrency`. Cloudflare treats this as a real visitor and populates the edge cache at the POP closest to the runner's egress IP.
4. Writes a markdown summary table to `$GITHUB_STEP_SUMMARY`.

## Quick start

Local dev:

```bash
uv sync --extra dev
uv run python -m preloader --config config/sites.yml --dry-run     # discover URLs only
uv run python -m preloader --config config/sites.yml --site iem    # run one site
uv run pytest -q
```

GitHub Actions:

- Cron: hourly, plus manual `workflow_dispatch` (with optional `site` and `dry_run` inputs).
- See `.github/workflows/preload.yml`.

## Configuration

See [`config/sites.example.yml`](config/sites.example.yml) for every supported field.

Minimal example:

```yaml
defaults:
  fetcher: httpx
  concurrency: 10
sites:
  - name: my-site
    sitemap_urls:
      - https://example.com/sitemap.xml
```

Per-site overrides are supported for every default. Environment-variable interpolation works in headers (`Authorization: "Bearer ${MY_TOKEN}"`).

### Fetchers

- `httpx` (default) — async HTTP/2 with browser headers. ~100–300 ms/URL.
- `playwright` — opt-in per site for sites with bot protection. Lazy-imported; only installed in CI when at least one site uses it. ~2–3 s/URL.

`curl-impersonate` was deprecated in Feb 2025 and Cloudflare now detects its TLS fingerprint, so it isn't included. Use `playwright` if you hit bot challenges.

## Region targeting (India / SEA)

GitHub-hosted runners are in US Azure regions. Cloudflare warms the POP closest to the **egress IP**, so this v1 warms US POPs, not Indian/SEA ones.

To warm Indian/SEA POPs, register a self-hosted GitHub Actions runner on a small VPS in Mumbai or Singapore (DigitalOcean, Hetzner, Oracle Free Tier all work) and add a second job to `preload.yml`:

```yaml
preload-india:
  runs-on: [self-hosted, mumbai]
  # ... same steps as `preload`
```

GitHub-hosted larger runners in South India / Southeast Asia exist (announced Apr 2024) but require a paid Team/Enterprise plan.

## Project layout

```
src/preloader/
  config.py     # pydantic + YAML loader, env interpolation
  sitemap.py    # recursive sitemap walker, gz support
  fetcher.py    # HttpxFetcher + lazy PlaywrightFetcher
  runner.py     # async orchestrator → SiteReport
  summary.py    # markdown renderer for $GITHUB_STEP_SUMMARY
  cli.py        # argparse entry: --config / --site / --dry-run
config/sites.yml          # active config
config/sites.example.yml  # documented example
.github/workflows/
  preload.yml   # scheduled cron job
  ci.yml        # tests + lint on push/PR
```
