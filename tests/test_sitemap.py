from __future__ import annotations

import re

import httpx
import pytest
import respx

from preloader.sitemap import discover_urls


@pytest.mark.asyncio
@respx.mock
async def test_flat_urlset(fx_urlset: bytes) -> None:
    respx.get("https://example.com/sitemap.xml").mock(return_value=httpx.Response(200, content=fx_urlset))
    async with httpx.AsyncClient() as c:
        result = await discover_urls(c, ["https://example.com/sitemap.xml"], [], 1000)
    assert result.urls == [
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
    ]
    assert result.errors == []


@pytest.mark.asyncio
@respx.mock
async def test_nested_index_with_gzip(
    fx_sitemapindex: bytes, fx_sub1: bytes, fx_sub2_gz: bytes
) -> None:
    respx.get("https://example.com/index.xml").mock(return_value=httpx.Response(200, content=fx_sitemapindex))
    respx.get("https://example.com/sub1.xml").mock(return_value=httpx.Response(200, content=fx_sub1))
    respx.get("https://example.com/sub2.xml.gz").mock(return_value=httpx.Response(200, content=fx_sub2_gz))
    async with httpx.AsyncClient() as c:
        result = await discover_urls(c, ["https://example.com/index.xml"], [], 1000)
    assert sorted(result.urls) == ["https://example.com/x", "https://example.com/y", "https://example.com/z"]


@pytest.mark.asyncio
@respx.mock
async def test_filter(fx_urlset: bytes) -> None:
    respx.get("https://example.com/sitemap.xml").mock(return_value=httpx.Response(200, content=fx_urlset))
    async with httpx.AsyncClient() as c:
        result = await discover_urls(c, ["https://example.com/sitemap.xml"], [re.compile(r"/[ab]$")], 1000)
    assert result.urls == ["https://example.com/a", "https://example.com/b"]


@pytest.mark.asyncio
@respx.mock
async def test_max_urls(fx_urlset: bytes) -> None:
    respx.get("https://example.com/sitemap.xml").mock(return_value=httpx.Response(200, content=fx_urlset))
    async with httpx.AsyncClient() as c:
        result = await discover_urls(c, ["https://example.com/sitemap.xml"], [], 2)
    assert len(result.urls) == 2


@pytest.mark.asyncio
@respx.mock
async def test_404_records_error() -> None:
    respx.get("https://example.com/missing.xml").mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as c:
        result = await discover_urls(c, ["https://example.com/missing.xml"], [], 1000)
    assert result.urls == []
    assert len(result.errors) == 1


@pytest.mark.asyncio
@respx.mock
async def test_malformed_xml() -> None:
    respx.get("https://example.com/broken.xml").mock(return_value=httpx.Response(200, content=b"<not-xml"))
    async with httpx.AsyncClient() as c:
        result = await discover_urls(c, ["https://example.com/broken.xml"], [], 1000)
    assert result.urls == []
    assert any("xml parse error" in msg for _, msg in result.errors)
