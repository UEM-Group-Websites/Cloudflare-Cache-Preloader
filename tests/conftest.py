from __future__ import annotations

import gzip
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fx_urlset() -> bytes:
    return (FIXTURES / "urlset.xml").read_bytes()


@pytest.fixture
def fx_sitemapindex() -> bytes:
    return (FIXTURES / "sitemapindex.xml").read_bytes()


@pytest.fixture
def fx_sub1() -> bytes:
    return (FIXTURES / "sub1.xml").read_bytes()


@pytest.fixture
def fx_sub2_gz() -> bytes:
    body = b"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/z</loc></url>
</urlset>"""
    return gzip.compress(body)
