"""Deterministic, offline tests for tools/zip_stager.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import make_zip
from tools.zip_stager import repo_name_from_filename, stage_uploaded_zip, stage_zip_bytes


def test_repo_name_from_filename_strips_zip_suffix():
    assert repo_name_from_filename("hello.zip") == "hello"


def test_repo_name_from_filename_discards_path_components():
    assert repo_name_from_filename("../../evil.zip") == "evil"


def test_repo_name_from_filename_keeps_dots_and_dashes():
    assert repo_name_from_filename("my-repo.v2.zip") == "my-repo.v2"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        ".zip",
        "...zip",  # strips to ".." -> forbidden
        "/",
        "weird name.zip",
    ],
)
def test_repo_name_from_filename_rejects_bad_names(bad):
    with pytest.raises(ValueError):
        repo_name_from_filename(bad)


def test_stage_zip_bytes_writes_and_extracts(tmp_path: Path):
    stage = tmp_path / "stage"
    stage.mkdir()
    data = make_zip({"README.md": b"# hi\n"})

    repo_dir = stage_zip_bytes(data, "hello", stage_dir=stage)

    assert repo_dir == stage / "hello"
    assert (repo_dir / "hello.zip").is_file()
    assert (repo_dir / "README.md").read_bytes() == b"# hi\n"


def test_stage_zip_bytes_existing_repo_raises(tmp_path: Path):
    stage = tmp_path / "stage"
    (stage / "hello").mkdir(parents=True)
    with pytest.raises(FileExistsError):
        stage_zip_bytes(make_zip({"a.txt": b"a"}), "hello", stage_dir=stage)


def test_stage_zip_bytes_rolls_back_on_bad_zip(tmp_path: Path):
    stage = tmp_path / "stage"
    stage.mkdir()
    with pytest.raises(Exception):
        stage_zip_bytes(b"not a zip file", "broken", stage_dir=stage)
    assert not (stage / "broken").exists()


def test_stage_uploaded_zip_end_to_end(tmp_path: Path):
    stage = tmp_path / "stage"
    stage.mkdir()
    data = make_zip({"README.md": b"# hi\n"})

    repo_dir = stage_uploaded_zip(data, "hello.zip", stage_dir=stage)

    assert repo_dir == stage / "hello"
    assert (repo_dir / "README.md").read_bytes() == b"# hi\n"
