from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from preloader.runner import SiteReport

# Cloudflare cache statuses we want columns for, in order.
PRIMARY_COLS = ["HIT", "MISS", "EXPIRED", "REVALIDATED", "DYNAMIC", "BYPASS"]


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(seconds), 60)
    return f"{m}m {s}s"


def _hit_rate(report: SiteReport) -> str:
    if report.fetched == 0:
        return "—"
    hit = report.by_cf_status.get("HIT", 0)
    return f"{(hit / report.fetched) * 100:.0f}%"


def render(reports: list[SiteReport], now: dt.datetime | None = None) -> str:
    now = now or dt.datetime.now(dt.UTC)
    lines: list[str] = []
    lines.append(f"# Cloudflare Cache Preload — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("")

    header = (
        "| Site | Discovered | Fetched | "
        + " | ".join(PRIMARY_COLS)
        + " | Other | HIT% | Errors | Duration |"
    )
    sep = "|---|" + "---:|" * (len(PRIMARY_COLS) + 6)
    lines.append(header)
    lines.append(sep)

    grand = {c: 0 for c in PRIMARY_COLS}
    grand_other = 0
    grand_discovered = 0
    grand_fetched = 0
    grand_errors = 0

    for r in reports:
        primary_counts = [r.by_cf_status.get(c, 0) for c in PRIMARY_COLS]
        other = sum(v for k, v in r.by_cf_status.items() if k not in PRIMARY_COLS)
        for k, v in zip(PRIMARY_COLS, primary_counts, strict=True):
            grand[k] += v
        grand_other += other
        grand_discovered += r.discovered
        grand_fetched += r.fetched
        grand_errors += len(r.errors)

        cells = [
            r.name,
            f"{r.discovered:,}",
            f"{r.fetched:,}",
            *(f"{v:,}" for v in primary_counts),
            f"{other:,}",
            _hit_rate(r),
            f"{len(r.errors):,}",
            _fmt_duration(r.elapsed_s),
        ]
        lines.append("| " + " | ".join(cells) + " |")

    if len(reports) > 1:
        total_hit = grand["HIT"]
        total_hit_rate = f"{(total_hit / grand_fetched) * 100:.0f}%" if grand_fetched else "—"
        cells = [
            "**Total**",
            f"**{grand_discovered:,}**",
            f"**{grand_fetched:,}**",
            *(f"**{grand[c]:,}**" for c in PRIMARY_COLS),
            f"**{grand_other:,}**",
            f"**{total_hit_rate}**",
            f"**{grand_errors:,}**",
            "",
        ]
        lines.append("| " + " | ".join(cells) + " |")

    lines.append("")

    sitemap_problem_sites = [r for r in reports if r.sitemap_errors]
    if sitemap_problem_sites:
        lines.append("## Sitemap warnings")
        lines.append("")
        for r in sitemap_problem_sites:
            lines.append(f"**{r.name}**")
            for url, msg in r.sitemap_errors:
                lines.append(f"- `{url}` — {msg}")
            lines.append("")

    error_sites = [r for r in reports if r.errors]
    if error_sites:
        total = sum(len(r.errors) for r in error_sites)
        lines.append(f"<details><summary>Fetch errors ({total})</summary>")
        lines.append("")
        for r in error_sites:
            lines.append(f"### {r.name} ({len(r.errors)})")
            lines.append("")
            for url, msg in r.errors[:50]:
                lines.append(f"- `{url}` — {msg}")
            if len(r.errors) > 50:
                lines.append(f"- _…and {len(r.errors) - 50} more_")
            lines.append("")
        lines.append("</details>")
        lines.append("")

    return "\n".join(lines)


def write(reports: list[SiteReport]) -> str:
    body = render(reports)
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with Path(target).open("a", encoding="utf-8") as f:
            f.write(body)
            f.write("\n")
    return body
