from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Protocol

import httpx

from preloader.config import ResolvedSite

logger = logging.getLogger(__name__)


class _MinGap:
    """Serialises callers so consecutive `wait()` returns are at least `min_gap_s` apart."""

    def __init__(self, min_gap_s: float) -> None:
        self._min_gap = min_gap_s
        self._lock = asyncio.Lock()
        self._last: float = 0.0

    async def wait(self) -> None:
        if self._min_gap <= 0:
            return
        async with self._lock:
            loop = asyncio.get_running_loop()
            wait = self._last + self._min_gap - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = loop.time()


class RateLimitedTransport(httpx.AsyncBaseTransport):
    """Wraps an httpx transport and enforces a minimum gap between every outbound HTTP
    request — including redirect follow-ups — so rate-limiting fires at the HTTP layer
    rather than only once per logical URL."""

    def __init__(self, inner: httpx.AsyncBaseTransport, min_gap_s: float) -> None:
        self._inner = inner
        self._gap = _MinGap(min_gap_s)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await self._gap.wait()
        return await self._inner.handle_async_request(request)

    async def aclose(self) -> None:
        await self._inner.aclose()


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


# Statuses worth retrying: rate-limit and transient gateway/WAF responses. Other 4xx are final.
RETRY_STATUSES = frozenset({429, 502, 503, 504})
MAX_RETRY_AFTER_S = 60.0
_BACKOFF_BASE_S = 0.5


def parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header (delta-seconds or HTTP-date) into seconds, or None."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _retry_delay(attempt: int, retry_after: float | None) -> float:
    if retry_after is not None:
        return min(retry_after, MAX_RETRY_AFTER_S)
    return _BACKOFF_BASE_S * (2**attempt) * random.uniform(0.5, 1.5)  # nosec B311 - jitter, not crypto


async def _sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _is_transient(exc: Exception) -> bool:
    # httpx.InvalidURL, TooManyRedirects etc. would fail identically on every attempt.
    if isinstance(exc, httpx.InvalidURL):
        return False
    if isinstance(exc, httpx.HTTPError):
        return isinstance(exc, httpx.TransportError)
    return True


# One attempt: returns (status, cf-cache-status, Retry-After header) or raises.
_Attempt = Callable[[str], Awaitable[tuple[int | None, str, str | None]]]


async def _fetch_with_retry(url: str, retries: int, once: _Attempt) -> FetchResult:
    """Retry policy shared by both fetchers. Never raises: any per-URL failure becomes
    a FetchResult with `error` set, so one bad URL can't take down the site's run."""
    start = time.monotonic()
    # Placeholder only: config validation guarantees retries >= 0, so the loop always overwrites it.
    last = FetchResult(url, None, "NONE", 0, "not attempted")
    for attempt in range(retries + 1):
        retry_after: float | None = None
        try:
            status, cf, retry_after_raw = await once(url)
        except Exception as e:
            last = FetchResult(url, None, "NONE", 0, f"{type(e).__name__}: {e}")
            if not _is_transient(e):
                break
        else:
            err = None if status is not None and 200 <= status < 400 else f"HTTP {status}" if status else "no response"
            last = FetchResult(url, status, cf, 0, err)
            if status not in RETRY_STATUSES:
                break
            retry_after = parse_retry_after(retry_after_raw)
        if attempt < retries:
            await _sleep(_retry_delay(attempt, retry_after))
    last.elapsed_ms = int((time.monotonic() - start) * 1000)
    return last


class HttpxFetcher:
    def __init__(self, site: ResolvedSite) -> None:
        self.site = site
        min_gap = site.request_delay_ms / 1000.0
        transport = RateLimitedTransport(httpx.AsyncHTTPTransport(http2=True), min_gap)
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=site.timeout_seconds,
            headers=site.headers,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=site.concurrency * 2),
        )

    async def _fetch_once(self, url: str) -> tuple[int | None, str, str | None]:
        resp = await self._client.get(url)
        return (
            resp.status_code,
            _bucket_status(resp.headers.get("cf-cache-status")),
            resp.headers.get("retry-after"),
        )

    async def fetch(self, url: str) -> FetchResult:
        return await _fetch_with_retry(url, self.site.retry_attempts, self._fetch_once)

    async def aclose(self) -> None:
        await self._client.aclose()


# Resource types the cache preloader never needs; scripts stay allowed because bot
# challenges need JS to pass.
BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font", "stylesheet"})


class PlaywrightFetcher:
    """Lazy-imported Playwright fetcher for sites with bot protection."""

    def __init__(self, site: ResolvedSite) -> None:
        self.site = site
        self._pw = None
        self._browser = None
        self._context = None
        self._init_lock = asyncio.Lock()
        self._gap = _MinGap(site.request_delay_ms / 1000.0)

    async def _ensure(self) -> None:
        if self._context is not None:
            return
        async with self._init_lock:
            if self._context is not None:
                return
            try:
                from playwright.async_api import async_playwright

                self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(headless=True)
                context = await self._browser.new_context(
                    user_agent=self.site.user_agent,
                    extra_http_headers={k: v for k, v in self.site.headers.items() if k.lower() != "user-agent"},
                )
                await context.route("**/*", self._route)
                self._context = context
            except BaseException:
                await self._teardown()
                raise

    @staticmethod
    async def _route(route) -> None:
        if route.request.resource_type in BLOCKED_RESOURCE_TYPES:
            await route.abort()
        else:
            await route.continue_()

    async def _fetch_once(self, url: str) -> tuple[int | None, str, str | None]:
        assert self._context is not None
        await self._gap.wait()
        page = await self._context.new_page()
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=int(self.site.timeout_seconds * 1000))
            if resp is None:
                return None, "NONE", None
            return resp.status, _bucket_status(resp.headers.get("cf-cache-status")), resp.headers.get("retry-after")
        finally:
            await page.close()

    async def fetch(self, url: str) -> FetchResult:
        try:
            await self._ensure()
        except Exception as e:
            return FetchResult(url, None, "NONE", 0, f"{type(e).__name__}: {e}")
        return await _fetch_with_retry(url, self.site.retry_attempts, self._fetch_once)

    async def _teardown(self) -> None:
        context, browser, pw = self._context, self._browser, self._pw
        self._context = self._browser = self._pw = None
        for closer in (
            context and context.close,
            browser and browser.close,
            pw and pw.stop,
        ):
            if closer is None:
                continue
            try:
                await closer()
            except Exception:
                logger.debug("playwright teardown step failed", exc_info=True)

    async def aclose(self) -> None:
        await self._teardown()


def make_fetcher(site: ResolvedSite) -> Fetcher:
    if site.fetcher == "httpx":
        return HttpxFetcher(site)
    if site.fetcher == "playwright":
        return PlaywrightFetcher(site)
    raise ValueError(f"unknown fetcher: {site.fetcher}")
