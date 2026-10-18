from __future__ import annotations

import asyncio
import sys
import types

import pytest

from preloader import fetcher as fetcher_mod
from preloader.config import Config
from preloader.fetcher import PlaywrightFetcher


def _site(**defaults):
    cfg = Config.model_validate(
        {
            "defaults": {"retry_attempts": 1, "concurrency": 4, "request_delay_ms": 0, **defaults},
            "sites": [{"name": "t", "sitemap_urls": ["https://e/s.xml"], "fetcher": "playwright"}],
        }
    )
    return cfg.resolve()[0]


class _Resp:
    def __init__(self, status: int, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self.headers = headers or {}


class _Page:
    def __init__(self, ctx: _Context) -> None:
        self.ctx = ctx

    async def goto(self, url: str, **_kw) -> _Resp:
        self.ctx.goto_times.append(asyncio.get_running_loop().time())
        return self.ctx.responses.pop(0) if self.ctx.responses else _Resp(200)

    async def close(self) -> None:
        pass


class _Context:
    def __init__(self) -> None:
        self.responses: list[_Resp] = []
        self.goto_times: list[float] = []
        self.route_handler = None
        self.closed = False

    async def route(self, pattern: str, handler) -> None:
        assert pattern == "**/*"
        self.route_handler = handler

    async def new_page(self) -> _Page:
        return _Page(self)

    async def close(self) -> None:
        self.closed = True


class _Browser:
    def __init__(self, stub: _Stub) -> None:
        self.stub = stub
        self.closed = False

    async def new_context(self, **_kw) -> _Context:
        if self.stub.fail_context:
            raise RuntimeError("context failed")
        ctx = _Context()
        self.stub.contexts.append(ctx)
        return ctx

    async def close(self) -> None:
        self.closed = True


class _Driver:
    def __init__(self, stub: _Stub) -> None:
        self.stub = stub
        self.stopped = False
        self.chromium = self

    async def launch(self, **_kw) -> _Browser:
        await asyncio.sleep(0)  # yield so concurrent callers interleave
        b = _Browser(self.stub)
        self.stub.browsers.append(b)
        return b

    async def stop(self) -> None:
        self.stopped = True


class _Stub:
    def __init__(self) -> None:
        self.drivers: list[_Driver] = []
        self.browsers: list[_Browser] = []
        self.contexts: list[_Context] = []
        self.fail_context = False

    def async_playwright(self):
        stub = self

        class _Starter:
            async def start(self) -> _Driver:
                await asyncio.sleep(0)
                d = _Driver(stub)
                stub.drivers.append(d)
                return d

        return _Starter()


@pytest.fixture
def pw(monkeypatch: pytest.MonkeyPatch) -> _Stub:
    stub = _Stub()
    mod = types.ModuleType("playwright.async_api")
    mod.async_playwright = stub.async_playwright
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", mod)

    async def _no_sleep(_s: float) -> None:
        pass

    monkeypatch.setattr(fetcher_mod, "_sleep", _no_sleep)
    return stub


@pytest.mark.asyncio
async def test_concurrent_first_fetches_launch_one_browser(pw: _Stub) -> None:
    f = PlaywrightFetcher(_site())
    results = await asyncio.gather(*(f.fetch(f"https://x.test/{i}") for i in range(6)))
    assert all(r.ok for r in results)
    assert len(pw.drivers) == 1
    assert len(pw.browsers) == 1
    await f.aclose()
    assert pw.drivers[0].stopped and pw.browsers[0].closed


@pytest.mark.asyncio
async def test_failed_init_cleans_up_and_reports_error(pw: _Stub) -> None:
    pw.fail_context = True
    f = PlaywrightFetcher(_site())
    r = await f.fetch("https://x.test/a")
    assert r.status_code is None
    assert r.error and "context failed" in r.error
    assert pw.drivers[0].stopped and pw.browsers[0].closed

    pw.fail_context = False  # a later call starts over cleanly
    r = await f.fetch("https://x.test/a")
    assert r.ok
    assert len(pw.drivers) == 2
    await f.aclose()


@pytest.mark.asyncio
async def test_request_delay_enforced_between_navigations(pw: _Stub) -> None:
    f = PlaywrightFetcher(_site(request_delay_ms=50))
    await asyncio.gather(*(f.fetch(f"https://x.test/{i}") for i in range(3)))
    times = pw.contexts[0].goto_times
    assert len(times) == 3
    assert times[1] - times[0] >= 0.04
    assert times[2] - times[1] >= 0.04
    await f.aclose()


@pytest.mark.asyncio
async def test_blocks_non_document_resources(pw: _Stub) -> None:
    f = PlaywrightFetcher(_site())
    await f.fetch("https://x.test/a")
    handler = pw.contexts[0].route_handler

    class _Route:
        def __init__(self, rtype: str) -> None:
            self.request = types.SimpleNamespace(resource_type=rtype)
            self.action = None

        async def abort(self) -> None:
            self.action = "abort"

        async def continue_(self) -> None:
            self.action = "continue"

    for rtype in ("image", "media", "font", "stylesheet"):
        r = _Route(rtype)
        await handler(r)
        assert r.action == "abort", rtype
    for rtype in ("document", "script", "xhr", "fetch"):
        r = _Route(rtype)
        await handler(r)
        assert r.action == "continue", rtype
    await f.aclose()


@pytest.mark.asyncio
async def test_503_retried_and_404_not(pw: _Stub) -> None:
    f = PlaywrightFetcher(_site())
    await f.fetch("https://x.test/warm")
    ctx = pw.contexts[0]
    ctx.goto_times.clear()

    ctx.responses = [_Resp(503, {"retry-after": "3"}), _Resp(200)]
    r = await f.fetch("https://x.test/a")
    assert r.ok and len(ctx.goto_times) == 2

    ctx.goto_times.clear()
    ctx.responses = [_Resp(404)]
    r = await f.fetch("https://x.test/b")
    assert r.error == "HTTP 404" and len(ctx.goto_times) == 1
    await f.aclose()
