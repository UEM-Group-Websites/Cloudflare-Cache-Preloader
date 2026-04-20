from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from preloader import summary
from preloader.config import load_config
from preloader.runner import run


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="cloudflare-preloader")
    p.add_argument("--config", "-c", required=True, help="Path to YAML config")
    p.add_argument("--site", "-s", help="Run only the named site")
    p.add_argument("--dry-run", action="store_true", help="Discover URLs but don't fetch")
    p.add_argument("--max-urls", type=int, help="Override max URLs per site")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    cfg = load_config(args.config)
    sites = cfg.resolve()

    if args.site:
        sites = [s for s in sites if s.name == args.site]
        if not sites:
            print(f"No site named {args.site!r} in config", file=sys.stderr)
            return 2

    if args.max_urls is not None:
        for s in sites:
            s.max_urls = args.max_urls

    reports = asyncio.run(run(sites, dry_run=args.dry_run))
    body = summary.write(reports)
    print(body)

    # Exit non-zero only if every site failed to fetch anything.
    any_success = any(r.fetched > 0 for r in reports) or args.dry_run
    return 0 if any_success else 1
