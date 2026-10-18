from __future__ import annotations

import pytest

from preloader import runner
from preloader.config import Config
from preloader.fetcher import FetchResult
from preloader.sitemap import SitemapResult

URLS = ["https://a.test/ok", "https://[::1/bad", "https://a.test/ok2"]


def _site():
    cfg = Config.model_validate({"sites": [{"name": "t", "sitemap_urls": ["https://e/s.xml"]}]})
    return cfg.resolve()[0]


class _ExplodingFetcher:
    async def fetch(self, url: str) -> FetchResult:
        if "bad" in url:
            raise ValueError("boom")
        return FetchResult(url=url, status_code=200, cf_cache_status="HIT", elapsed_ms=1)

    async def aclose(self) -> None:
        pass


@pytest.mark.asyncio
async def test_one_raising_fetch_keeps_other_results(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _discover(*_a, **_k) -> SitemapResult:
        return SitemapResult(urls=list(URLS))

    monkeypatch.setattr(runner, "discover_urls", _discover)
    monkeypatch.setattr(runner, "make_fetcher", lambda _s: _ExplodingFetcher())

    [report] = await runner.run([_site()])

    assert report.discovered == 3
    assert report.fetched == 3
    assert report.by_cf_status["HIT"] == 2
    assert report.errors == [("https://[::1/bad", "ValueError: boom")]


@pytest.mark.asyncio
async def test_crashed_site_fallback_does_not_use_site_name_as_url(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _crash(*_a, **_k):
        raise RuntimeError("kaput")

    monkeypatch.setattr(runner, "_run_site", _crash)

    [report] = await runner.run([_site()])

    assert report.name == "t"
    assert report.errors == []
    assert report.sitemap_errors == [("(site run crashed)", "RuntimeError: kaput")]
