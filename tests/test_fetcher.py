from __future__ import annotations

import time as _time

import httpx
import pytest
import respx

from preloader import fetcher as fetcher_mod
from preloader.config import Config
from preloader.fetcher import HttpxFetcher, RateLimitedTransport, parse_retry_after


def _site():
    cfg = Config.model_validate(
        {
            "defaults": {"retry_attempts": 1, "concurrency": 2, "timeout_seconds": 5},
            "sites": [{"name": "t", "sitemap_urls": ["https://e/s.xml"]}],
        }
    )
    return cfg.resolve()[0]


@pytest.mark.asyncio
@respx.mock
async def test_success_extracts_cf_status() -> None:
    respx.get("https://example.com/page").mock(return_value=httpx.Response(200, headers={"cf-cache-status": "HIT"}))
    f = HttpxFetcher(_site())
    try:
        r = await f.fetch("https://example.com/page")
    finally:
        await f.aclose()
    assert r.status_code == 200
    assert r.cf_cache_status == "HIT"
    assert r.ok
    assert r.error is None


@pytest.mark.asyncio
@respx.mock
async def test_missing_header_buckets_as_none() -> None:
    respx.get("https://example.com/page").mock(return_value=httpx.Response(200))
    f = HttpxFetcher(_site())
    try:
        r = await f.fetch("https://example.com/page")
    finally:
        await f.aclose()
    assert r.cf_cache_status == "NONE"


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []

    async def _fake(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(fetcher_mod, "_sleep", _fake)
    return recorded


@pytest.mark.asyncio
@respx.mock
async def test_5xx_marks_error(sleeps: list[float]) -> None:
    route = respx.get("https://example.com/page").mock(return_value=httpx.Response(503))
    f = HttpxFetcher(_site())
    try:
        r = await f.fetch("https://example.com/page")
    finally:
        await f.aclose()
    assert route.call_count == 2  # 503 is retried, then still reported as an error
    assert r.status_code == 503
    assert not r.ok
    assert r.error == "HTTP 503"


@pytest.mark.asyncio
@respx.mock
async def test_404_is_not_retried(sleeps: list[float]) -> None:
    route = respx.get("https://example.com/page").mock(return_value=httpx.Response(404))
    f = HttpxFetcher(_site())
    try:
        r = await f.fetch("https://example.com/page")
    finally:
        await f.aclose()
    assert route.call_count == 1
    assert r.error == "HTTP 404"
    assert sleeps == []


@pytest.mark.asyncio
@respx.mock
async def test_503_then_200_honours_retry_after(sleeps: list[float]) -> None:
    route = respx.get("https://example.com/page").mock(
        side_effect=[httpx.Response(503, headers={"Retry-After": "7"}), httpx.Response(200)]
    )
    f = HttpxFetcher(_site())
    try:
        r = await f.fetch("https://example.com/page")
    finally:
        await f.aclose()
    assert route.call_count == 2
    assert r.ok
    assert sleeps == [7.0]


@pytest.mark.asyncio
@respx.mock
async def test_retry_after_is_capped(sleeps: list[float]) -> None:
    respx.get("https://example.com/page").mock(
        side_effect=[httpx.Response(429, headers={"Retry-After": "3600"}), httpx.Response(200)]
    )
    f = HttpxFetcher(_site())
    try:
        await f.fetch("https://example.com/page")
    finally:
        await f.aclose()
    assert sleeps == [fetcher_mod.MAX_RETRY_AFTER_S]


@pytest.mark.asyncio
@respx.mock
async def test_backoff_without_retry_after_has_jitter(sleeps: list[float]) -> None:
    respx.get("https://example.com/page").mock(side_effect=[httpx.Response(502), httpx.Response(200)])
    f = HttpxFetcher(_site())
    try:
        await f.fetch("https://example.com/page")
    finally:
        await f.aclose()
    assert len(sleeps) == 1
    assert 0.25 <= sleeps[0] <= 0.75


@pytest.mark.asyncio
async def test_invalid_url_becomes_error_result(sleeps: list[float]) -> None:
    f = HttpxFetcher(_site())
    try:
        r = await f.fetch("https://[::1/bad")
    finally:
        await f.aclose()
    assert r.status_code is None
    assert r.error and "InvalidURL" in r.error
    assert sleeps == []  # not transient, so not retried


def test_parse_retry_after() -> None:
    assert parse_retry_after("12") == 12.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("garbage") is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0  # past date
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    future = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    assert 25 <= (parse_retry_after(future) or 0) <= 30


@pytest.mark.asyncio
@respx.mock
async def test_timeout_retries_then_fails(sleeps: list[float]) -> None:
    route = respx.get("https://example.com/page").mock(side_effect=httpx.ConnectTimeout("boom"))
    f = HttpxFetcher(_site())
    try:
        r = await f.fetch("https://example.com/page")
    finally:
        await f.aclose()
    assert route.call_count == 2  # retry_attempts=1 → 2 total attempts
    assert r.status_code is None
    assert r.error and "ConnectTimeout" in r.error


@pytest.mark.asyncio
async def test_rate_limited_transport_enforces_gap() -> None:
    """Each request through RateLimitedTransport must be separated by at least min_gap."""
    timestamps: list[float] = []
    min_gap = 0.05  # 50 ms

    class _RecordingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            timestamps.append(_time.monotonic())
            return httpx.Response(200)

    transport = RateLimitedTransport(_RecordingTransport(), min_gap_s=min_gap)
    async with httpx.AsyncClient(transport=transport) as client:
        for path in ("/a", "/b", "/c"):
            await client.get(f"https://example.com{path}")

    assert len(timestamps) == 3
    tolerance = 0.01  # allow 10 ms clock jitter
    assert timestamps[1] - timestamps[0] >= min_gap - tolerance
    assert timestamps[2] - timestamps[1] >= min_gap - tolerance


async def test_non_httpx_exceptions_are_retried(sleeps: list[float]) -> None:
    # Playwright navigation timeouts / net::ERR_* errors aren't httpx exceptions but are transient.
    calls = 0

    async def once(url: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("navigation timeout")
        return 200, "HIT", None

    r = await fetcher_mod._fetch_with_retry("https://e/p", 2, once)
    assert calls == 2
    assert r.ok
    assert len(sleeps) == 1
