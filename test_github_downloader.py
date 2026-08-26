"""Deterministic, offline tests for tools/github_downloader.py."""

from __future__ import annotations

import urllib.error
from pathlib import Path

import pytest

import tools.github_downloader as gd
from conftest import make_zip
from tools.github_downloader import (
    _resolve_stage,
    _zip_url,
    download_repo,
    download_zip,
    extract_zip,
    parse_repo,
)


def test_parse_repo_plain():
    assert parse_repo("https://github.com/octo/hello") == ("octo", "hello")


def test_parse_repo_with_git_suffix_and_slash():
    assert parse_repo("https://github.com/octo/hello.git/") == ("octo", "hello")


def test_parse_repo_with_www():
    assert parse_repo("http://www.github.com/octo/hello") == ("octo", "hello")


@pytest.mark.parametrize(
    "bad",
    [
        "https://gitlab.com/octo/hello",
        "https://github.com/octo",
        "https://github.com/octo/hello/tree/main",
        "not a url",
        "https://github.com/../..",
        "https://github.com/octo/../secret",
    ],
)
def test_parse_repo_rejects_bad_urls(bad):
    with pytest.raises(ValueError):
        parse_repo(bad)


def test_zip_url_defaults_to_head():
    assert _zip_url("octo", "hello") == "https://codeload.github.com/octo/hello/zip/HEAD"


def test_zip_url_explicit_branch():
    assert _zip_url("octo", "hello", "main") == (
        "https://codeload.github.com/octo/hello/zip/main"
    )


def test_extract_zip_strips_top_level_dir(tmp_path: Path):
    dest = tmp_path / "hello"
    data = make_zip(
        {
            "hello-main/README.md": b"# hello\n",
            "hello-main/src/main.py": b"print('hi')\n",
        }
    )
    extract_zip(data, dest)
    assert (dest / "README.md").read_bytes() == b"# hello\n"
    assert (dest / "src" / "main.py").read_bytes() == b"print('hi')\n"
    assert not (dest / "hello-main").exists()


def test_extract_zip_flat_archive(tmp_path: Path):
    dest = tmp_path / "flat"
    extract_zip(make_zip({"a.txt": b"a"}), dest)
    assert (dest / "a.txt").read_bytes() == b"a"


def test_extract_zip_rejects_zip_slip(tmp_path: Path):
    dest = tmp_path / "safe"
    data = make_zip({"../../evil.txt": b"owned"})
    with pytest.raises(ValueError):
        extract_zip(data, dest)
    assert not (tmp_path / "evil.txt").exists()


def test_resolve_stage_missing_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        _resolve_stage(tmp_path / "nope")


def test_download_zip_saves_inside_repo_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    stage = tmp_path / "stage"
    stage.mkdir()
    monkeypatch.setattr(gd, "_fetch", lambda url: make_zip({"hello-main/README.md": b"# hi\n"}))

    zip_path = download_zip("https://github.com/octo/hello", stage_dir=stage)

    assert zip_path == stage / "hello" / "hello.zip"
    assert zip_path.is_file()


def test_download_zip_existing_repo_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    stage = tmp_path / "stage"
    (stage / "hello").mkdir(parents=True)
    monkeypatch.setattr(gd, "_fetch", lambda url: b"")
    with pytest.raises(FileExistsError):
        download_zip("https://github.com/octo/hello", stage_dir=stage)


def test_download_zip_rolls_back_on_fetch_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    stage = tmp_path / "stage"
    stage.mkdir()

    def _boom(url):
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

    monkeypatch.setattr(gd, "_fetch", _boom)

    with pytest.raises(urllib.error.HTTPError):
        download_zip("https://github.com/octo/missing", stage_dir=stage)

    assert not (stage / "missing").exists()


def test_download_repo_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    stage = tmp_path / "stage"
    stage.mkdir()
    monkeypatch.setattr(
        gd, "_fetch", lambda url: make_zip({"hello-main/README.md": b"# hi\n"})
    )

    repo_dir = download_repo("https://github.com/octo/hello", stage_dir=stage)

    assert repo_dir == stage / "hello"
    assert (repo_dir / "README.md").read_bytes() == b"# hi\n"
    assert (repo_dir / "hello.zip").is_file()


def test_download_repo_existing_dir_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    stage = tmp_path / "stage"
    (stage / "hello").mkdir(parents=True)
    monkeypatch.setattr(gd, "_fetch", lambda url: b"")
    with pytest.raises(FileExistsError):
        download_repo("https://github.com/octo/hello", stage_dir=stage)


def test_download_repo_rolls_back_on_bad_zip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    stage = tmp_path / "stage"
    stage.mkdir()
    monkeypatch.setattr(gd, "_fetch", lambda url: b"not a zip file")
    with pytest.raises(Exception):
        download_repo("https://github.com/octo/broken", stage_dir=stage)
    assert not (stage / "broken").exists()
