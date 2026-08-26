"""Shared test fixtures/helpers for the zdocs-ai test suite."""

from __future__ import annotations

import io
import zipfile


def make_zip(entries: dict[str, bytes]) -> bytes:
    """Build in-memory zip bytes from a ``{name: content}`` mapping."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buf.getvalue()
