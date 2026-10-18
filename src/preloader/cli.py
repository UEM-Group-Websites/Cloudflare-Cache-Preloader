from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import yaml
from pydantic import ValidationError

from preloader import summary
from preloader.config import load_config
from preloader.runner import run


def _positive_int(value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not an integer") from None
    if n < 1:
        raise argparse.ArgumentTypeError(f"{value!r} must be a positive integer")
    return n


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="cloudflare-preloader")
    p.add_argument("--config", "-c", required=True, help="Path to YAML config")
    p.add_argument("--site", "-s", help="Run only the named site")
    p.add_argument("--dry-run", action="store_true", help="Discover URLs but don't fetch")
    p.add_argument("--max-urls", type=_positive_int, help="Override max URLs per site")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    try:
        cfg = load_config(args.config)
    except (OSError, yaml.YAMLError, ValidationError) as e:
        print(f"Invalid config {args.config}: {e}", file=sys.stderr)
        return 2
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

    # Exit non-zero if any site crashed (nothing discovered, errors recorded), or — outside a dry run —
    # if any site that discovered URLs got no successful fetch. One blocked site shouldn't hide behind healthy ones.
    crashed = any(r.discovered == 0 and r.errors for r in reports)
    all_failed = not args.dry_run and any(r.discovered > 0 and r.succeeded == 0 for r in reports)
    return 1 if crashed or all_failed else 0
