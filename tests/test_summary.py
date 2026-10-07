from __future__ import annotations

import datetime as dt
from collections import Counter

from preloader.runner import SiteReport
from preloader.summary import render


def test_render_includes_per_site_and_totals() -> None:
    reports = [
        SiteReport(
            name="alpha",
            discovered=10,
            fetched=10,
            by_cf_status=Counter({"HIT": 7, "MISS": 3}),
            elapsed_s=12.3,
        ),
        SiteReport(
            name="beta",
            discovered=5,
            fetched=4,
            by_cf_status=Counter({"DYNAMIC": 4}),
            errors=[("https://beta/x", "HTTP 502")],
            elapsed_s=3.0,
        ),
    ]
    body = render(reports, now=dt.datetime(2026, 4, 20, 10, 30, tzinfo=dt.UTC))
    assert "Cloudflare Cache Preload — 2026-04-20 10:30 UTC" in body
    assert "| alpha |" in body
    assert "| beta |" in body
    assert "**Total**" in body
    assert "Fetch errors (1)" in body
    assert "https://beta/x" in body


def test_render_handles_empty_reports() -> None:
    body = render([], now=dt.datetime(2026, 4, 20, 10, 30, tzinfo=dt.UTC))
    assert "Cloudflare Cache Preload" in body


def test_render_handles_zero_fetched() -> None:
    body = render(
        [SiteReport(name="x", discovered=0, fetched=0)],
        now=dt.datetime(2026, 4, 20, tzinfo=dt.UTC),
    )
    assert "| x |" in body
    assert "—" in body  # hit rate when fetched=0


def test_render_flags_sites_that_ran_out_of_time() -> None:
    reports = [
        SiteReport(
            name="slow", discovered=3295, fetched=2100, budget_skipped=1195, budget_exhausted=True, elapsed_s=2400.0
        ),
        SiteReport(name="stuck", budget_exhausted=True, elapsed_s=2400.0),
        SiteReport(name="fast", discovered=10, fetched=10, elapsed_s=5.0),
    ]
    body = render(reports, now=dt.datetime(2026, 4, 20, tzinfo=dt.UTC))
    assert "## Time budget reached" in body
    assert "**slow** — stopped after 40m 0s; 1,195 of 3,295 URLs not fetched" in body
    assert "**fast**" not in body
    assert "**stuck** — stopped during sitemap discovery after 40m 0s" in body


def test_render_omits_budget_section_when_all_sites_finish() -> None:
    body = render(
        [SiteReport(name="x", discovered=1, fetched=1, elapsed_s=1.0)],
        now=dt.datetime(2026, 4, 20, tzinfo=dt.UTC),
    )
    assert "Time budget reached" not in body
