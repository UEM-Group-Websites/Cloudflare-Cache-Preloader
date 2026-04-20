from __future__ import annotations

import httpx
import pytest
import respx

from preloader.config import Config
from preloader.fetcher import HttpxFetcher


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
