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
