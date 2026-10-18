from __future__ import annotations

import pytest

from preloader import cli
from preloader.runner import SiteReport

CONFIG = """\
sites:
  - name: a
    sitemap_urls: [https://a.test/s.xml]
  - name: b
    sitemap_urls: [https://b.test/s.xml]
"""


@pytest.fixture
def config_path(tmp_path) -> str:
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG)
    return str(p)


@pytest.fixture
def fake_run(monkeypatch: pytest.MonkeyPatch):
    """Patch cli.run to return canned reports; records the sites/dry_run it was called with."""
    state: dict = {"reports": [], "calls": []}

    async def _run(sites, dry_run=False):
        state["calls"].append((sites, dry_run))
        return state["reports"]

    monkeypatch.setattr(cli, "run", _run)
    monkeypatch.setattr(cli.summary, "write", lambda reports: "summary")
    return state


def _ok(name: str, discovered: int = 2, errors: int = 0) -> SiteReport:
    return SiteReport(name=name, discovered=discovered, fetched=discovered, errors=[("u", "HTTP 503")] * errors)


def test_succeeded_excludes_errors() -> None:
    assert _ok("a", discovered=5, errors=2).succeeded == 3


def test_exit_0_when_all_succeed(config_path, fake_run) -> None:
    fake_run["reports"] = [_ok("a"), _ok("b")]
    assert cli.main(["-c", config_path]) == 0


def test_exit_1_when_every_fetch_fails(config_path, fake_run) -> None:
    fake_run["reports"] = [_ok("a", errors=2), _ok("b", errors=2)]
    assert cli.main(["-c", config_path]) == 1


def test_exit_1_when_one_site_fully_fails(config_path, fake_run) -> None:
    fake_run["reports"] = [_ok("a"), _ok("b", errors=2)]
    assert cli.main(["-c", config_path]) == 1


def test_partial_errors_still_exit_0(config_path, fake_run) -> None:
    fake_run["reports"] = [_ok("a", errors=1), _ok("b")]
    assert cli.main(["-c", config_path]) == 0


def test_site_with_nothing_discovered_is_not_a_failure(config_path, fake_run) -> None:
    fake_run["reports"] = [_ok("a"), SiteReport(name="b")]
    assert cli.main(["-c", config_path]) == 0


def test_exit_1_when_site_crashed(config_path, fake_run) -> None:
    fake_run["reports"] = [
        _ok("a"),
        SiteReport(name="b", sitemap_errors=[("(site run crashed)", "RuntimeError: boom")]),
    ]
    assert cli.main(["-c", config_path]) == 1


def test_exit_1_when_every_sitemap_failed(config_path, fake_run) -> None:
    fake_run["reports"] = [_ok("a"), SiteReport(name="b", sitemap_errors=[("https://b.test/s.xml", "fetch failed")])]
    assert cli.main(["--config", config_path]) == 1


def test_dry_run_exits_0(config_path, fake_run) -> None:
    fake_run["reports"] = [
        SiteReport(name="a", discovered=3, skipped=True),
        SiteReport(name="b", discovered=3, skipped=True),
    ]
    assert cli.main(["-c", config_path, "--dry-run"]) == 0
    assert fake_run["calls"][0][1] is True


def test_dry_run_exits_1_when_site_crashed(config_path, fake_run) -> None:
    fake_run["reports"] = [SiteReport(name="a", sitemap_errors=[("(site run crashed)", "RuntimeError: boom")])]
    assert cli.main(["-c", config_path, "--dry-run"]) == 1


def test_site_filter(config_path, fake_run) -> None:
    fake_run["reports"] = [_ok("b")]
    assert cli.main(["-c", config_path, "--site", "b"]) == 0
    assert [s.name for s in fake_run["calls"][0][0]] == ["b"]


def test_unknown_site_exits_2(config_path, fake_run, capsys) -> None:
    assert cli.main(["-c", config_path, "--site", "nope"]) == 2
    assert "nope" in capsys.readouterr().err
    assert fake_run["calls"] == []


def test_max_urls_override(config_path, fake_run) -> None:
    fake_run["reports"] = [_ok("a"), _ok("b")]
    cli.main(["-c", config_path, "--max-urls", "7"])
    assert [s.max_urls for s in fake_run["calls"][0][0]] == [7, 7]


@pytest.mark.parametrize("value", ["0", "-5", "abc"])
def test_max_urls_must_be_positive(config_path, fake_run, value, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["-c", config_path, "--max-urls", value])
    assert exc.value.code == 2
    assert fake_run["calls"] == []


def test_invalid_config_prints_message_and_exits_2(tmp_path, fake_run, capsys) -> None:
    p = tmp_path / "bad.yaml"
    p.write_text("defaults:\n  concurrency: 0\nsites:\n  - name: a\n    sitemap_urls: [https://a.test/s.xml]\n")
    assert cli.main(["-c", str(p)]) == 2
    err = capsys.readouterr().err
    assert "concurrency" in err
    assert "Traceback" not in err


def test_non_utf8_config_exits_2(tmp_path, fake_run, capsys) -> None:
    p = tmp_path / "bad.yaml"
    p.write_bytes(b"sites: []\n\xff\xfe")
    assert cli.main(["-c", str(p)]) == 2
    assert "Traceback" not in capsys.readouterr().err


def test_missing_config_file_exits_2(tmp_path, fake_run, capsys) -> None:
    assert cli.main(["-c", str(tmp_path / "missing.yaml")]) == 2
    assert capsys.readouterr().err
