from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

FetcherKind = Literal["httpx", "playwright"]

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS: dict[str, str] = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}


# Only the braced ${VAR} form is interpolated, so a literal $ in a cookie or token is left alone.
_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def _interpolate_env(value: str) -> str:
    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in os.environ:
            raise ValueError(f"environment variable {name} is not set (referenced as ${{{name}}} in headers)")
        return os.environ[name]

    return _ENV_PATTERN.sub(repl, value)


def _check_filters(patterns: list[str] | None) -> list[str] | None:
    for p in patterns or []:
        try:
            re.compile(p)
        except re.error as e:
            raise ValueError(f"invalid sitemap_url_filters regex {p!r}: {e}") from e
    return patterns


class Defaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fetcher: FetcherKind = "httpx"
    user_agent: str = DEFAULT_USER_AGENT
    headers: dict[str, str] = Field(default_factory=lambda: dict(DEFAULT_HEADERS))
    concurrency: int = Field(default=3, ge=1)
    timeout_seconds: float = Field(default=20.0, gt=0)
    retry_attempts: int = Field(default=2, ge=0)
    max_urls_per_site: int = Field(default=5000, ge=1)
    sitemap_url_filters: list[str] = Field(default_factory=list)
    # 250 ms between requests per worker keeps ~12 req/s across 3 workers,
    # well under Wordfence's default 240 pages/min block threshold.
    request_delay_ms: int = Field(default=250, ge=0)
    # Wall-clock cap per site (discovery + fetching). When it runs out, unfetched URLs are
    # skipped and a partial report is still written. Keep it below the job's timeout-minutes.
    time_budget_seconds: float | None = Field(default=None, gt=0)

    @field_validator("headers", mode="after")
    @classmethod
    def _interpolate_headers(cls, v: dict[str, str]) -> dict[str, str]:
        return {k: _interpolate_env(val) for k, val in v.items()}

    _validate_filters = field_validator("sitemap_url_filters", mode="after")(_check_filters)


class Site(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    sitemap_urls: list[str] = Field(min_length=1)
    fetcher: FetcherKind | None = None
    user_agent: str | None = None
    headers: dict[str, str] | None = None
    concurrency: int | None = Field(default=None, ge=1)
    timeout_seconds: float | None = Field(default=None, gt=0)
    retry_attempts: int | None = Field(default=None, ge=0)
    max_urls: int | None = Field(default=None, ge=1)
    sitemap_url_filters: list[str] | None = None
    request_delay_ms: int | None = Field(default=None, ge=0)
    time_budget_seconds: float | None = Field(default=None, gt=0)

    @field_validator("headers", mode="after")
    @classmethod
    def _interpolate_headers(cls, v: dict[str, str] | None) -> dict[str, str] | None:
        if v is None:
            return None
        return {k: _interpolate_env(val) for k, val in v.items()}

    _validate_filters = field_validator("sitemap_url_filters", mode="after")(_check_filters)


class ResolvedSite(BaseModel):
    """Per-site config after merging defaults — what runner.py consumes."""

    model_config = ConfigDict(extra="forbid")

    name: str
    sitemap_urls: list[str]
    fetcher: FetcherKind
    user_agent: str
    headers: dict[str, str]
    concurrency: int
    timeout_seconds: float
    retry_attempts: int
    max_urls: int
    sitemap_url_filters: list[re.Pattern[str]]
    request_delay_ms: int
    time_budget_seconds: float | None


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    defaults: Defaults = Field(default_factory=Defaults)
    sites: list[Site] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_site_names(self) -> Config:
        seen: set[str] = set()
        dupes = sorted({s.name for s in self.sites if s.name in seen or seen.add(s.name)})
        if dupes:
            raise ValueError(f"duplicate site names: {', '.join(dupes)}")
        return self

    def resolve(self) -> list[ResolvedSite]:
        resolved: list[ResolvedSite] = []
        d = self.defaults
        for s in self.sites:
            merged_headers = dict(d.headers)
            if s.headers:
                merged_headers.update(s.headers)
            # Always inject User-Agent into headers for the fetcher.
            ua = s.user_agent or d.user_agent
            merged_headers["User-Agent"] = ua

            filters_raw = s.sitemap_url_filters if s.sitemap_url_filters is not None else d.sitemap_url_filters
            filters = [re.compile(p) for p in filters_raw]

            def _pick(site_val, default_val):
                return default_val if site_val is None else site_val

            resolved.append(
                ResolvedSite(
                    name=s.name,
                    sitemap_urls=s.sitemap_urls,
                    fetcher=_pick(s.fetcher, d.fetcher),
                    user_agent=ua,
                    headers=merged_headers,
                    concurrency=_pick(s.concurrency, d.concurrency),
                    timeout_seconds=_pick(s.timeout_seconds, d.timeout_seconds),
                    retry_attempts=_pick(s.retry_attempts, d.retry_attempts),
                    max_urls=_pick(s.max_urls, d.max_urls_per_site),
                    sitemap_url_filters=filters,
                    request_delay_ms=_pick(s.request_delay_ms, d.request_delay_ms),
                    time_budget_seconds=_pick(s.time_budget_seconds, d.time_budget_seconds),
                )
            )
        return resolved


def load_config(path: str | Path) -> Config:
    text = Path(path).read_text(encoding="utf-8")
    raw = yaml.safe_load(text) or {}
    return Config.model_validate(raw)
