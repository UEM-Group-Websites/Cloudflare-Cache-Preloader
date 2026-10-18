from __future__ import annotations

import re

import pytest
from pydantic import ValidationError

from preloader.config import DEFAULT_HEADERS, Config, load_config

SITE = {"name": "a", "sitemap_urls": ["https://a.test/sitemap.xml"]}


def _cfg(defaults: dict | None = None, sites: list[dict] | None = None) -> Config:
    return Config.model_validate({"defaults": defaults or {}, "sites": sites or [SITE]})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("concurrency", 0),
        ("max_urls_per_site", 0),
        ("retry_attempts", -1),
        ("request_delay_ms", -100),
        ("timeout_seconds", 0),
        ("timeout_seconds", -1),
    ],
)
def test_defaults_reject_out_of_range(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        _cfg(defaults={field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("concurrency", 0),
        ("max_urls", -5),
        ("retry_attempts", -1),
        ("request_delay_ms", -1),
        ("timeout_seconds", 0),
    ],
)
def test_site_rejects_out_of_range(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        _cfg(sites=[{**SITE, field: value}])


def test_zero_retries_and_delay_allowed() -> None:
    site = _cfg(defaults={"retry_attempts": 0, "request_delay_ms": 0}).resolve()[0]
    assert (site.retry_attempts, site.request_delay_ms) == (0, 0)


def test_invalid_filter_regex_names_pattern() -> None:
    with pytest.raises(ValidationError, match=r"\(unclosed"):
        _cfg(defaults={"sitemap_url_filters": ["(unclosed"]})
    with pytest.raises(ValidationError, match=r"\[bad"):
        _cfg(sites=[{**SITE, "sitemap_url_filters": ["[bad"]}])


def test_duplicate_site_names_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate"):
        _cfg(sites=[SITE, {**SITE, "sitemap_urls": ["https://b.test/s.xml"]}])


def test_env_interpolation_braced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_TOKEN", "s3cret")
    cfg = _cfg(defaults={"headers": {"Authorization": "Bearer ${MY_TOKEN}"}})
    assert cfg.defaults.headers["Authorization"] == "Bearer s3cret"


def test_env_literal_dollar_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ABC", "x")
    cfg = _cfg(defaults={"headers": {"Cookie": "a=$ABC; b=${FOO; c=$FOO}"}})
    assert cfg.defaults.headers["Cookie"] == "a=$ABC; b=${FOO; c=$FOO}"


def test_env_missing_variable_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MISSING_VAR", raising=False)
    with pytest.raises(ValidationError, match="MISSING_VAR"):
        _cfg(defaults={"headers": {"Authorization": "Bearer ${MISSING_VAR}"}})
    with pytest.raises(ValidationError, match="MISSING_VAR"):
        _cfg(sites=[{**SITE, "headers": {"X": "${MISSING_VAR}"}}])


def test_resolve_merges_headers_and_injects_user_agent() -> None:
    cfg = _cfg(
        defaults={"user_agent": "default-ua"},
        sites=[{**SITE, "headers": {"X-Site": "1", "Accept-Language": "de"}}],
    )
    h = cfg.resolve()[0].headers
    assert h["X-Site"] == "1"
    assert h["Accept-Language"] == "de"  # site overrides default
    assert h["Accept"] == DEFAULT_HEADERS["Accept"]  # untouched defaults survive
    assert h["User-Agent"] == "default-ua"


def test_resolve_site_user_agent_wins() -> None:
    cfg = _cfg(defaults={"user_agent": "default-ua"}, sites=[{**SITE, "user_agent": "site-ua"}])
    site = cfg.resolve()[0]
    assert site.user_agent == "site-ua"
    assert site.headers["User-Agent"] == "site-ua"


def test_resolve_filter_override_precedence() -> None:
    cfg = _cfg(
        defaults={"sitemap_url_filters": ["/default/"]},
        sites=[
            SITE,
            {**SITE, "name": "b", "sitemap_url_filters": ["/own/"]},
            {**SITE, "name": "c", "sitemap_url_filters": []},
        ],
    )
    a, b, c = cfg.resolve()
    assert [p.pattern for p in a.sitemap_url_filters] == ["/default/"]
    assert [p.pattern for p in b.sitemap_url_filters] == ["/own/"]
    assert c.sitemap_url_filters == []  # explicit empty list overrides defaults
    assert all(isinstance(p, re.Pattern) for p in a.sitemap_url_filters)


def test_resolve_numeric_overrides() -> None:
    cfg = _cfg(defaults={"concurrency": 3, "max_urls_per_site": 100}, sites=[{**SITE, "concurrency": 7, "max_urls": 5}])
    site = cfg.resolve()[0]
    assert (site.concurrency, site.max_urls) == (7, 5)


def test_load_config_from_yaml(tmp_path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("sites:\n  - name: a\n    sitemap_urls: [https://a.test/s.xml]\n")
    assert load_config(p).sites[0].name == "a"
