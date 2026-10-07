from __future__ import annotations

import asyncio

import pytest

from preloader import runner
from preloader.config import Config
from preloader.fetcher import FetchResult
from preloader.sitemap import SitemapResult

URLS = [f"https://example.com/{i}" for i in range(10)]


def _site(time_budget_seconds: float | None):
    defaults = {"concurrency": 2}
    if time_budget_seconds is not None:
        defaults["time_budget_seconds"] = time_budget_seconds
    cfg = Config.model_validate({"defaults": defaults, "sites": [{"name": "t", "sitemap_urls": ["https://e/s.xml"]}]})
    return cfg.resolve()[0]


class _SlowFetcher:
    def __init__(self, delay_s: float) -> None:
        self.delay_s = delay_s
        self.closed = False

    async def fetch(self, url: str) -> FetchResult:
        await asyncio.sleep(self.delay_s)
        return FetchResult(url=url, status_code=200, cf_cache_status="HIT", elapsed_ms=int(self.delay_s * 1000))

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def slow_fetcher(monkeypatch: pytest.MonkeyPatch) -> _SlowFetcher:
    fetcher = _SlowFetcher(delay_s=0.05)

    async def _discover(*_args, **_kwargs) -> SitemapResult:
        return SitemapResult(urls=list(URLS))

    monkeypatch.setattr(runner, "discover_urls", _discover)
    monkeypatch.setattr(runner, "make_fetcher", lambda _site: fetcher)
    return fetcher


async def test_without_budget_fetches_everything(slow_fetcher: _SlowFetcher) -> None:
    [report] = await runner.run([_site(None)])
    assert report.fetched == len(URLS)
    assert report.budget_skipped == 0
    assert slow_fetcher.closed


async def test_budget_stops_fetching_and_reports_partial_results(slow_fetcher: _SlowFetcher) -> None:
    # 10 URLs × 50 ms at concurrency 2 needs ~250 ms; a 120 ms budget fits only a few batches.
    [report] = await runner.run([_site(0.12)])
    assert 0 < report.fetched < len(URLS)
    assert report.budget_skipped == len(URLS) - report.fetched
    assert report.by_cf_status["HIT"] == report.fetched
    assert report.errors == []
    assert report.elapsed_s < 0.2
    assert slow_fetcher.closed


def test_site_overrides_default_budget() -> None:
    cfg = Config.model_validate(
        {
            "defaults": {"time_budget_seconds": 2400},
            "sites": [
                {"name": "a", "sitemap_urls": ["https://a/s.xml"]},
                {"name": "b", "sitemap_urls": ["https://b/s.xml"], "time_budget_seconds": 600},
            ],
        }
    )
    a, b = cfg.resolve()
    assert a.time_budget_seconds == 2400
    assert b.time_budget_seconds == 600


def test_budget_must_be_positive() -> None:
    with pytest.raises(ValueError):
        Config.model_validate({"defaults": {"time_budget_seconds": 0}, "sites": [{"name": "a", "sitemap_urls": ["x"]}]})
