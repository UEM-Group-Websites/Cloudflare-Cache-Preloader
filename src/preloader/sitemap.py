from __future__ import annotations

import logging
import re
import zlib
from dataclasses import dataclass, field

import httpx
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException

logger = logging.getLogger(__name__)

_NS = re.compile(r"^\{[^}]+\}")
_MAX_DEPTH = 3
_MAX_SITEMAP_BYTES = 50 * 1024 * 1024  # sitemap protocol limit, uncompressed
_GZIP_MAGIC = b"\x1f\x8b"


def _strip_ns(tag: str) -> str:
    return _NS.sub("", tag)


class _SitemapError(Exception):
    """A sitemap that could not be fetched or decoded; the message goes into SitemapResult.errors."""


def _too_large() -> _SitemapError:
    return _SitemapError(f"sitemap too large (over {_MAX_SITEMAP_BYTES} bytes uncompressed)")


def _maybe_gunzip(content: bytes) -> bytes:
    """Decompress a gzip file served as-is (e.g. sitemap.xml.gz), member by member, under the size cap.
    Content-Encoding: gzip never reaches here — httpx decodes it while streaming in _fetch_one."""
    if content[:2] != _GZIP_MAGIC:
        return content
    out = bytearray()
    data = content
    try:
        # A valid gzip file may hold several concatenated members.
        while data[:2] == _GZIP_MAGIC:
            d = zlib.decompressobj(wbits=31)
            out += d.decompress(data, _MAX_SITEMAP_BYTES + 1 - len(out))
            if len(out) > _MAX_SITEMAP_BYTES:
                raise _too_large()
            if not d.eof:
                raise EOFError("truncated gzip data")
            data = d.unused_data
    except (EOFError, zlib.error) as e:
        raise _SitemapError(f"gzip decompress failed: {e}") from e
    return bytes(out)


@dataclass
class SitemapResult:
    urls: list[str] = field(default_factory=list)
    errors: list[tuple[str, str]] = field(default_factory=list)
    seen_urls: set[str] = field(default_factory=set, repr=False)


async def _fetch_one(client: httpx.AsyncClient, url: str) -> tuple[bytes, str]:
    """Return (body, final URL after redirects); raise _SitemapError on any fetch/decode failure."""
    try:
        async with client.stream("GET", url, follow_redirects=True) as resp:
            resp.raise_for_status()
            # Stream so the size cap also bounds Content-Encoding: gzip, which httpx decodes chunk by chunk.
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > _MAX_SITEMAP_BYTES:
                    raise _too_large()
                chunks.append(chunk)
            final_url = str(resp.url)
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        logger.warning("sitemap fetch failed: %s — %s", url, e)
        raise _SitemapError("fetch failed") from e
    return _maybe_gunzip(b"".join(chunks)), final_url


async def _walk(
    client: httpx.AsyncClient,
    sitemap_url: str,
    depth: int,
    seen_sitemaps: set[str],
    out: SitemapResult,
    filters: list[re.Pattern[str]],
    max_urls: int,
) -> None:
    if len(out.urls) >= max_urls:
        return
    if depth > _MAX_DEPTH:
        out.errors.append((sitemap_url, f"max sitemap depth {_MAX_DEPTH} exceeded"))
        return
    if sitemap_url in seen_sitemaps:
        return
    seen_sitemaps.add(sitemap_url)

    try:
        body, final_url = await _fetch_one(client, sitemap_url)
    except _SitemapError as e:
        out.errors.append((sitemap_url, str(e)))
        # Forget the failure so a later fallback that redirects here can still be parsed.
        seen_sitemaps.discard(sitemap_url)
        return
    if final_url != sitemap_url:
        if final_url in seen_sitemaps:
            return
        seen_sitemaps.add(final_url)

    try:
        root = ET.fromstring(body)
    except (ET.ParseError, DefusedXmlException) as e:
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
            # Sequential: keeps request rate bounded and lets max_urls stop further fetches.
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
            if url in out.seen_urls:
                continue
            if filters and not any(p.search(url) for p in filters):
                continue
            out.seen_urls.add(url)
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
    """Try sitemap_urls in order and use the first that yields any URLs; earlier failures are
    only logged. Errors are reported only if no entry yields URLs and none parsed cleanly
    (a valid but empty sitemap means there is legitimately nothing to fetch)."""
    seen: set[str] = set()
    failed: list[tuple[str, str]] = []
    clean_empty = False
    for sm in sitemap_urls:
        out = SitemapResult()
        await _walk(client, sm, 1, seen, out, filters, max_urls)
        if out.urls:
            for url, msg in failed:
                logger.info("sitemap %s failed before fallback succeeded: %s", url, msg)
            return out
        clean_empty = clean_empty or not out.errors
        failed.extend(out.errors)
    if clean_empty:
        for url, msg in failed:
            logger.info("sitemap %s failed, but another entry parsed with no URLs: %s", url, msg)
        return SitemapResult()
    return SitemapResult(errors=failed)
