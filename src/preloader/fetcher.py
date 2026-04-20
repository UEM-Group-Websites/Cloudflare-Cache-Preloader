from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from preloader.config import ResolvedSite

logger = logging.getLogger(__name__)


@dataclass
class FetchResult:
    url: str
    status_code: int | None
    cf_cache_status: str
    elapsed_ms: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status_code is not None and 200 <= self.status_code < 400


class Fetcher(Protocol):
    async def fetch(self, url: str) -> FetchResult: ...
    async def aclose(self) -> None: ...


def _bucket_status(raw: str | None) -> str:
    if not raw:
        return "NONE"
    return raw.strip().split(",")[0].upper()


class HttpxFetcher:
    def __init__(self, site: ResolvedSite) -> None:
        self.site = site
        self._client = httpx.AsyncClient(
            http2=True,
            timeout=site.timeout_seconds,
            headers=site.headers,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=site.concurrency * 2),
        )

    async def fetch(self, url: str) -> FetchResult:
        start = time.monotonic()
        last_err: str | None = None
        for attempt in range(self.site.retry_attempts + 1):
            try:
                resp = await self._client.get(url)
                elapsed = int((time.monotonic() - start) * 1000)
                return FetchResult(
                    url=url,
                    status_code=resp.status_code,
                    cf_cache_status=_bucket_status(resp.headers.get("cf-cache-status")),
                    elapsed_ms=elapsed,
                    error=None if 200 <= resp.status_code < 400 else f"HTTP {resp.status_code}",
                )
            except httpx.HTTPError as e:
                last_err = f"{type(e).__name__}: {e}"
                if attempt < self.site.retry_attempts:
                    await asyncio.sleep(0.5 * (2**attempt))
        elapsed = int((time.monotonic() - start) * 1000)
        return FetchResult(url=url, status_code=None, cf_cache_status="NONE", elapsed_ms=elapsed, error=last_err)

    async def aclose(self) -> None:
        await self._client.aclose()


class PlaywrightFetcher:
    """Lazy-imported Playwright fetcher for sites with bot protection."""

    def __init__(self, site: ResolvedSite) -> None:
        self.site = site
        self._pw = None
        self._browser = None
        self._context = None

    async def _ensure(self) -> None:
        if self._context is not None:
            return
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=True)
        self._context = await self._browser.new_context(
            user_agent=self.site.user_agent,
            extra_http_headers={k: v for k, v in self.site.headers.items() if k.lower() != "user-agent"},
        )

    async def _fetch_once(self, url: str) -> tuple[int | None, str, str | None]:
        assert self._context is not None
        page = await self._context.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=int(self.site.timeout_seconds * 1000))
            if resp is None:
                return None, "NONE", "no response"
            status = resp.status
            cf = _bucket_status(resp.headers.get("cf-cache-status"))
            err = None if 200 <= status < 400 else f"HTTP {status}"
            return status, cf, err
        finally:
            await page.close()

    async def fetch(self, url: str) -> FetchResult:
        await self._ensure()
        start = time.monotonic()
        last_err: str | None = None
        for attempt in range(self.site.retry_attempts + 1):
            try:
                status, cf, err = await self._fetch_once(url)
                # Only retry on transient network errors; bad HTTP codes return as-is.
                if err is None or (status is not None and status < 500):
                    return FetchResult(
                        url=url,
                        status_code=status,
                        cf_cache_status=cf,
                        elapsed_ms=int((time.monotonic() - start) * 1000),
                        error=err,
                    )
                last_err = err
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
            if attempt < self.site.retry_attempts:
                await asyncio.sleep(0.5 * (2**attempt))
        return FetchResult(
            url=url,
            status_code=None,
            cf_cache_status="NONE",
            elapsed_ms=int((time.monotonic() - start) * 1000),
            error=last_err,
        )

    async def aclose(self) -> None:
        if self._context is not None:
            await self._context.close()
        if self._browser is not None:
            await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()


def make_fetcher(site: ResolvedSite) -> Fetcher:
    if site.fetcher in ("httpx", "httpx_impersonate"):
        # NOTE: httpx_impersonate is a placeholder; v1 uses plain httpx with browser headers.
        return HttpxFetcher(site)
    if site.fetcher == "playwright":
        return PlaywrightFetcher(site)
    raise ValueError(f"unknown fetcher: {site.fetcher}")
