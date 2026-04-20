from __future__ import annotations

import gzip
import logging
import re
from dataclasses import dataclass, field

import httpx
from defusedxml import ElementTree as ET

logger = logging.getLogger(__name__)

_NS = re.compile(r"^\{[^}]+\}")
_MAX_DEPTH = 3


def _strip_ns(tag: str) -> str:
    return _NS.sub("", tag)


def _maybe_gunzip(content: bytes, url: str, content_encoding: str | None) -> bytes:
    if url.endswith(".gz") or (content_encoding and "gzip" in content_encoding.lower()):
        try:
            return gzip.decompress(content)
        except OSError:
            return content
    return content


@dataclass
class SitemapResult:
    urls: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)


async def _fetch_one(client: httpx.AsyncClient, url: str) -> bytes | None:
    try:
        resp = await client.get(url, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        logger.warning("sitemap fetch failed: %s — %s", url, e)
        return None
    return _maybe_gunzip(resp.content, url, resp.headers.get("Content-Encoding"))


async def _walk(
    client: httpx.AsyncClient,
    sitemap_url: str,
    depth: int,
    seen_sitemaps: set[str],
    out: SitemapResult,
    filters: list[re.Pattern[str]],
    max_urls: int,
) -> None:
    if depth > _MAX_DEPTH:
        out.errors.append((sitemap_url, f"max sitemap depth {_MAX_DEPTH} exceeded"))
        return
    if sitemap_url in seen_sitemaps:
        return
    seen_sitemaps.add(sitemap_url)

    body = await _fetch_one(client, sitemap_url)
    if body is None:
        out.errors.append((sitemap_url, "fetch failed"))
        return

    try:
        root = ET.fromstring(body)
    except ET.ParseError as e:
        out.errors.append((sitemap_url, f"xml parse error: {e}"))
        return

    tag = _strip_ns(root.tag)

    if tag == "sitemapindex":
        for sm in root:
            if _strip_ns(sm.tag) != "sitemap":
                continue
            loc_el = next((c for c in sm if _strip_ns(c.tag) == "loc"), None)
            if loc_el is None or not loc_el.text:
                continue
            await _walk(client, loc_el.text.strip(), depth + 1, seen_sitemaps, out, filters, max_urls)
            if len(out.urls) >= max_urls:
                return
    elif tag == "urlset":
        for u in root:
            if _strip_ns(u.tag) != "url":
                continue
            loc_el = next((c for c in u if _strip_ns(c.tag) == "loc"), None)
            if loc_el is None or not loc_el.text:
                continue
            url = loc_el.text.strip()
            if filters and not any(p.search(url) for p in filters):
                continue
            out.urls.append(url)
            if len(out.urls) >= max_urls:
                return
    else:
        out.errors.append((sitemap_url, f"unknown sitemap root tag: {tag}"))


async def discover_urls(
    client: httpx.AsyncClient,
    sitemap_urls: list[str],
    filters: list[re.Pattern[str]],
    max_urls: int,
) -> SitemapResult:
    out = SitemapResult()
    seen: set[str] = set()
    for sm in sitemap_urls:
        await _walk(client, sm, 1, seen, out, filters, max_urls)
        if len(out.urls) >= max_urls:
            break
    # Dedupe while preserving order.
    deduped: list[str] = []
    seen_urls: set[str] = set()
    for u in out.urls:
        if u not in seen_urls:
            seen_urls.add(u)
            deduped.append(u)
    out.urls = deduped[:max_urls]
    return out
