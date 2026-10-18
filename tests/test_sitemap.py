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


def _urlset(*paths: str) -> bytes:
    # Fixture XML from constant test paths; Semgrep's raw-html-format rule is a false positive here.
    # nosemgrep: python.django.security.injection.raw-html-format.raw-html-format
    locs = "".join(f"<url><loc>https://example.com/{p}</loc></url>" for p in paths)
    ns = "http://www.sitemaps.org/schemas/sitemap/0.9"
    # nosemgrep: python.django.security.injection.raw-html-format.raw-html-format
    return f'<?xml version="1.0"?><urlset xmlns="{ns}">{locs}</urlset>'.encode()


def _index(*names: str) -> bytes:
    locs = "".join(f"<sitemap><loc>{n}</loc></sitemap>" for n in names)
    return f'<?xml version="1.0"?><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</sitemapindex>'.encode()


@pytest.mark.asyncio
@respx.mock
async def test_defused_xml_rejection_is_recorded_and_others_continue() -> None:
    evil = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><urlset><url><loc>&a;</loc></url></urlset>'
    respx.get("https://example.com/index.xml").mock(
        return_value=httpx.Response(200, content=_index("https://example.com/evil.xml", "https://example.com/good.xml"))
    )
    respx.get("https://example.com/evil.xml").mock(return_value=httpx.Response(200, content=evil))
    respx.get("https://example.com/good.xml").mock(return_value=httpx.Response(200, content=_urlset("ok")))
    async with httpx.AsyncClient() as c:
        result = await discover_urls(c, ["https://example.com/index.xml"], [], 1000)
    assert result.urls == ["https://example.com/ok"]
    assert [u for u, _ in result.errors] == ["https://example.com/evil.xml"]


@pytest.mark.asyncio
@respx.mock
async def test_truncated_and_corrupt_gzip_are_recorded() -> None:
    import gzip

    full = gzip.compress(_urlset(*[f"p{i}" for i in range(200)]))
    respx.get("https://example.com/index.xml").mock(
        return_value=httpx.Response(
            200,
            content=_index(
                "https://example.com/trunc.xml.gz", "https://example.com/bad.xml.gz", "https://example.com/good.xml"
            ),
        )
    )
    respx.get("https://example.com/trunc.xml.gz").mock(return_value=httpx.Response(200, content=full[:-20]))
    respx.get("https://example.com/bad.xml.gz").mock(
        return_value=httpx.Response(200, content=b"\x1f\x8b\x08\x00" + b"garbage" * 10)
    )
    respx.get("https://example.com/good.xml").mock(return_value=httpx.Response(200, content=_urlset("ok")))
    async with httpx.AsyncClient() as c:
        result = await discover_urls(c, ["https://example.com/index.xml"], [], 1000)
    assert result.urls == ["https://example.com/ok"]
    assert {u for u, _ in result.errors} == {"https://example.com/trunc.xml.gz", "https://example.com/bad.xml.gz"}


@pytest.mark.asyncio
@respx.mock
async def test_decompression_bomb_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    import gzip

    from preloader import sitemap

    monkeypatch.setattr(sitemap, "_MAX_SITEMAP_BYTES", 1000)
    respx.get("https://example.com/bomb.xml.gz").mock(
        return_value=httpx.Response(200, content=gzip.compress(b"<urlset>" + b" " * 100_000))
    )
    async with httpx.AsyncClient() as c:
        result = await discover_urls(c, ["https://example.com/bomb.xml.gz"], [], 1000)
    assert result.urls == []
    assert any("too large" in msg for _, msg in result.errors)


@pytest.mark.asyncio
@respx.mock
async def test_invalid_loc_url_is_recorded() -> None:
    respx.get("https://example.com/index.xml").mock(
        return_value=httpx.Response(200, content=_index("http://example.com:abc/x.xml", "https://example.com/good.xml"))
    )
    respx.get("https://example.com/good.xml").mock(return_value=httpx.Response(200, content=_urlset("ok")))
    async with httpx.AsyncClient() as c:
        result = await discover_urls(c, ["https://example.com/index.xml"], [], 1000)
    assert result.urls == ["https://example.com/ok"]
    assert len(result.errors) == 1


@pytest.mark.asyncio
@respx.mock
async def test_sub_sitemaps_walked_sequentially_and_stop_at_cap() -> None:
    import asyncio

    active = peak = 0

    async def slow(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, content=_urlset(request.url.path.strip("/").replace(".xml", "")))

    names = [f"https://example.com/s{i}.xml" for i in range(10)]
    respx.get("https://example.com/index.xml").mock(return_value=httpx.Response(200, content=_index(*names)))
    route = respx.get(url__regex=r"https://example\.com/s\d+\.xml").mock(side_effect=slow)
    async with httpx.AsyncClient() as c:
        result = await discover_urls(c, ["https://example.com/index.xml"], [], 3)
    assert peak == 1
    assert len(result.urls) == 3
    assert route.call_count == 3


@pytest.mark.asyncio
@respx.mock
async def test_duplicates_do_not_count_toward_cap() -> None:
    respx.get("https://example.com/a.xml").mock(
        return_value=httpx.Response(200, content=_urlset(*[f"p{i}" for i in range(5)]))
    )
    respx.get("https://example.com/b.xml").mock(
        return_value=httpx.Response(200, content=_urlset(*[f"p{i}" for i in range(10)]))
    )
    respx.get("https://example.com/index.xml").mock(
        return_value=httpx.Response(200, content=_index("https://example.com/a.xml", "https://example.com/b.xml"))
    )
    async with httpx.AsyncClient() as c:
        result = await discover_urls(c, ["https://example.com/index.xml"], [], 8)
    assert result.urls == [f"https://example.com/p{i}" for i in range(8)]


@pytest.mark.asyncio
@respx.mock
async def test_sitemap_urls_fallback_stops_at_first_success_and_hides_earlier_errors() -> None:
    respx.get("https://example.com/missing.xml").mock(return_value=httpx.Response(404))
    good = respx.get("https://example.com/good.xml").mock(return_value=httpx.Response(200, content=_urlset("ok")))
    extra = respx.get("https://example.com/extra.xml").mock(return_value=httpx.Response(200, content=_urlset("no")))
    async with httpx.AsyncClient() as c:
        result = await discover_urls(
            c,
            ["https://example.com/missing.xml", "https://example.com/good.xml", "https://example.com/extra.xml"],
            [],
            1000,
        )
    assert result.urls == ["https://example.com/ok"]
    assert result.errors == []
    assert good.call_count == 1
    assert extra.call_count == 0


@pytest.mark.asyncio
@respx.mock
async def test_sitemap_urls_all_failing_reports_all_errors() -> None:
    respx.get("https://example.com/a.xml").mock(return_value=httpx.Response(404))
    respx.get("https://example.com/b.xml").mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as c:
        result = await discover_urls(c, ["https://example.com/a.xml", "https://example.com/b.xml"], [], 1000)
    assert result.urls == []
    assert [u for u, _ in result.errors] == ["https://example.com/a.xml", "https://example.com/b.xml"]


@pytest.mark.asyncio
@respx.mock
async def test_redirect_alias_not_parsed_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    from preloader import sitemap

    parsed: list[bytes] = []
    real = sitemap.ET.fromstring
    monkeypatch.setattr(sitemap.ET, "fromstring", lambda b: (parsed.append(b), real(b))[1])
    respx.get("https://example.com/index.xml").mock(
        return_value=httpx.Response(200, content=_index("https://example.com/a.xml", "https://example.com/alias.xml"))
    )
    respx.get("https://example.com/alias.xml").mock(
        return_value=httpx.Response(301, headers={"Location": "https://example.com/a.xml"})
    )
    target = respx.get("https://example.com/a.xml").mock(return_value=httpx.Response(200, content=_urlset("p")))
    async with httpx.AsyncClient() as c:
        result = await discover_urls(c, ["https://example.com/index.xml"], [], 1000)
    assert result.urls == ["https://example.com/p"]
    assert target.call_count == 2  # the alias redirect is still fetched
    assert len(parsed) == 2  # index + a.xml; the alias body is not parsed again
    assert result.errors == []
