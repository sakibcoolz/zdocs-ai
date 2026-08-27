"""Shared test fixtures/helpers for the zdocs-ai test suite."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

#: Small per-language repositories used by the operations tests. See
#: ``tests/fixtures/README.md`` — several tests assert exact counts, so keep
#: them stable.
FIXTURES_DIR = Path(__file__).resolve().parent / "tests" / "fixtures"


def make_zip(entries: dict[str, bytes]) -> bytes:
    """Build in-memory zip bytes from a ``{name: content}`` mapping."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()


def fixture_repo(name: str) -> Path:
    """Absolute path to a fixture repository under ``tests/fixtures/``."""
    path = FIXTURES_DIR / name
    if not path.is_dir():  # pragma: no cover - guards a mistyped fixture name
        raise FileNotFoundError(f"No such fixture repository: {name!r}")
    return path


@pytest.fixture()
def python_repo() -> Path:
    """Python fixture repository (ABC, Protocol, inheritance, composition)."""
    return fixture_repo("python_repo")


@pytest.fixture()
def go_repo() -> Path:
    """Go fixture repository (interface, embedding, structural satisfaction)."""
    return fixture_repo("go_repo")


@pytest.fixture()
def java_repo() -> Path:
    """Java fixture repository (extends/implements/@Override)."""
    return fixture_repo("java_repo")


@pytest.fixture()
def ts_repo() -> Path:
    """TypeScript fixture repository (interface/abstract/parameter properties)."""
    return fixture_repo("ts_repo")


@pytest.fixture()
def js_repo() -> Path:
    """JavaScript fixture repository (ES class inheritance, require)."""
    return fixture_repo("js_repo")


@pytest.fixture()
def all_fixtures_repo() -> Path:
    """The whole fixtures tree, treated as one multi-language repository."""
    return FIXTURES_DIR


class StubCommandRunner:
    """CommandRunner stand-in that reports every external tool as missing.

    Used to prove the pure-Python fallbacks work on a machine with no ripgrep,
    no git and no ast-grep — the "missing external tool" path that must never
    silently produce wrong results.
    """

    def __init__(self, available: set[str] | None = None) -> None:
        self.available = available or set()
        self.calls: list[list[str]] = []

    def is_available(self, program: str) -> bool:
        return program in self.available

    def require(self, program: str) -> None:
        from operations.errors import ToolUnavailableError

        if program not in self.available:
            raise ToolUnavailableError(
                f"Required command-line tool is not installed or not allowlisted: {program!r}"
            )

    def run(self, argv, *, cwd, timeout=None, check=False):  # noqa: ANN001
        self.calls.append(list(argv))
        self.require(argv[0])
        raise AssertionError("StubCommandRunner should not execute commands")
