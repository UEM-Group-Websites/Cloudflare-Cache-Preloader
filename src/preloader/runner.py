from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from dataclasses import dataclass, field

import httpx

from preloader.config import ResolvedSite
from preloader.fetcher import FetchResult, make_fetcher
from preloader.sitemap import discover_urls

logger = logging.getLogger(__name__)


@dataclass
class SiteReport:
    name: str
    discovered: int = 0
    fetched: int = 0
    by_cf_status: Counter[str] = field(default_factory=Counter)
    errors: list[tuple[str, str]] = field(default_factory=list)
    sitemap_errors: list[tuple[str, str]] = field(default_factory=list)
    elapsed_s: float = 0.0
    skipped: bool = False


async def _run_site(site: ResolvedSite, dry_run: bool) -> SiteReport:
    report = SiteReport(name=site.name)
    start = time.monotonic()

    discovery_headers = {**site.headers}
    async with httpx.AsyncClient(
        http2=True, timeout=site.timeout_seconds, headers=discovery_headers, follow_redirects=True
    ) as discovery_client:
        sm = await discover_urls(
            discovery_client,
            site.sitemap_urls,
            site.sitemap_url_filters,
            site.max_urls,
        )

    report.discovered = len(sm.urls)
    report.sitemap_errors = sm.errors

    logger.info("[%s] discovered %d URLs", site.name, report.discovered)

    if dry_run or report.discovered == 0:
        report.elapsed_s = time.monotonic() - start
        if dry_run:
            report.skipped = True
        return report

    fetcher = make_fetcher(site)
    sem = asyncio.Semaphore(site.concurrency)
    delay = site.request_delay_ms / 1000.0

    async def _bounded(url: str) -> FetchResult:
        async with sem:
            if delay:
                await asyncio.sleep(delay)
            return await fetcher.fetch(url)

    try:
        results = await asyncio.gather(*(_bounded(u) for u in sm.urls))
    finally:
        await fetcher.aclose()

    for r in results:
        report.fetched += 1
        report.by_cf_status[r.cf_cache_status] += 1
        if not r.ok:
            report.errors.append((r.url, r.error or f"HTTP {r.status_code}"))

    report.elapsed_s = time.monotonic() - start
    return report


async def run(sites: list[ResolvedSite], dry_run: bool = False) -> list[SiteReport]:
    async def _safe(site: ResolvedSite) -> SiteReport:
        try:
            return await _run_site(site, dry_run=dry_run)
        except Exception as e:
            logger.exception("[%s] unrecoverable error", site.name)
            return SiteReport(name=site.name, errors=[(site.name, f"{type(e).__name__}: {e}")])

    return list(await asyncio.gather(*(_safe(s) for s in sites)))
