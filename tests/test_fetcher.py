from __future__ import annotations

import time as _time

import httpx
import pytest
import respx

from preloader.config import Config
from preloader.fetcher import HttpxFetcher, RateLimitedTransport


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
    respx.get("https://example.com/page").mock(
        return_value=httpx.Response(200, headers={"cf-cache-status": "HIT"})
    )
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


@pytest.mark.asyncio
@respx.mock
async def test_5xx_marks_error() -> None:
    respx.get("https://example.com/page").mock(return_value=httpx.Response(503))
    f = HttpxFetcher(_site())
    try:
        r = await f.fetch("https://example.com/page")
    finally:
        await f.aclose()
    assert r.status_code == 503
    assert not r.ok
    assert r.error == "HTTP 503"


@pytest.mark.asyncio
@respx.mock
async def test_timeout_retries_then_fails() -> None:
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
