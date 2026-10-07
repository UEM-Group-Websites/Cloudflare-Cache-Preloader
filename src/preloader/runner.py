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
    budget_skipped: int = 0  # URLs left unfetched because the site's time budget ran out
    budget_exhausted: bool = False  # time budget ran out, during discovery or fetching


async def _run_site(site: ResolvedSite, dry_run: bool) -> SiteReport:
    report = SiteReport(name=site.name)
    start = time.monotonic()

    # One deadline covers discovery and fetching, so it maps directly onto the job's wall clock.
    deadline = None if site.time_budget_seconds is None else start + site.time_budget_seconds

    def _remaining() -> float | None:
        return None if deadline is None else max(0.0, deadline - time.monotonic())

    discovery_headers = {**site.headers}
    budget = asyncio.timeout(_remaining())
    try:
        async with (
            budget,
            httpx.AsyncClient(
                http2=True, timeout=site.timeout_seconds, headers=discovery_headers, follow_redirects=True
            ) as discovery_client,
        ):
            sm = await discover_urls(
                discovery_client,
                site.sitemap_urls,
                site.sitemap_url_filters,
                site.max_urls,
            )
    except TimeoutError:
        if not budget.expired():
            raise
        logger.warning("[%s] time budget of %ds reached during sitemap discovery", site.name, site.time_budget_seconds)
        report.budget_exhausted = True
        report.sitemap_errors = [(site.sitemap_urls[0], "time budget reached during sitemap discovery")]
        report.elapsed_s = time.monotonic() - start
        return report

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

    # Rate limiting is enforced at the HTTP-transport layer (RateLimitedTransport)
    # so every outbound request — including redirect follow-ups — respects the gap.
    # The semaphore here only caps the number of in-flight logical URLs.
    # Progress counts completions, not submissions, so [n/total] climbs steadily even with concurrency > 1.
    completed = 0
    width = len(str(report.discovered))

    async def _bounded(url: str) -> FetchResult:
        nonlocal completed
        async with sem:
            r = await fetcher.fetch(url)
        completed += 1
        logger.info(
            "[%s] [%*d/%d] %s %s %dms %s%s",
            site.name,
            width,
            completed,
            report.discovered,
            r.status_code if r.status_code is not None else "ERR",
            r.cf_cache_status,
            r.elapsed_ms,
            r.url,
            f" — {r.error}" if r.error else "",
        )
        return r

    tasks = [asyncio.create_task(_bounded(u)) for u in sm.urls]
    try:
        done, pending = await asyncio.wait(tasks, timeout=_remaining())
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    finally:
        await fetcher.aclose()

    results = [t.result() for t in tasks if t in done]
    report.budget_skipped = len(pending)
    report.budget_exhausted = bool(pending)
    if pending:
        logger.warning(
            "[%s] time budget of %ds reached — skipped %d of %d URLs",
            site.name,
            site.time_budget_seconds,
            len(pending),
            report.discovered,
        )

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

    # Sites run in parallel — each site's Wordfence / WAF rate limit is independent,
    # and the per-site RateLimitedTransport in HttpxFetcher prevents per-site overrun.
    return list(await asyncio.gather(*(_safe(s) for s in sites)))
