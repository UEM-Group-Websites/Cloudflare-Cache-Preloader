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
async def test_nested_index_with_gzip(fx_sitemapindex: bytes, fx_sub1: bytes, fx_sub2_gz: bytes) -> None:
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


def _urlset(paths: list[str]) -> bytes:
    entries = "".join(f"<url><loc>https://example.com{path}</loc></url>" for path in paths)
    return f"<urlset>{entries}</urlset>".encode()


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("filtered", [False, True])
@pytest.mark.asyncio
@respx.mock
async def test_max_urls_counts_unique_pages(nested: bool, filtered: bool) -> None:
    sitemap_urls = ["https://example.com/a.xml", "https://example.com/b.xml"]
    first = [f"/p{number}" for number in range(5)]
    second = [f"/p{number}" for number in range(10)]
    if filtered:
        first.insert(0, "/skip")
        second.insert(0, "/skip")
    respx.get(sitemap_urls[0]).mock(return_value=httpx.Response(200, content=_urlset(first)))
    respx.get(sitemap_urls[1]).mock(return_value=httpx.Response(200, content=_urlset(second)))
    if nested:
        entries = "".join(f"<sitemap><loc>{url}</loc></sitemap>" for url in sitemap_urls)
        respx.get("https://example.com/index.xml").mock(
            return_value=httpx.Response(200, content=f"<sitemapindex>{entries}</sitemapindex>".encode())
        )
        sitemap_urls = ["https://example.com/index.xml"]
    filters = [re.compile(r"/p\d+$")] if filtered else []

    async with httpx.AsyncClient() as client:
        result = await discover_urls(client, sitemap_urls, filters, 8)

    assert result.urls == [f"https://example.com/p{number}" for number in range(8)]
    assert result.errors == []


@pytest.mark.asyncio
@respx.mock
async def test_repeated_pages_in_one_sitemap_do_not_use_the_limit() -> None:
    respx.get("https://example.com/sitemap.xml").mock(
        return_value=httpx.Response(200, content=_urlset(["/a", "/a", "/b", "/b", "/c", "/d"]))
    )
    async with httpx.AsyncClient() as client:
        result = await discover_urls(client, ["https://example.com/sitemap.xml"], [], 3)

    assert result.urls == ["https://example.com/a", "https://example.com/b", "https://example.com/c"]
    assert result.errors == []
