"""Deterministic caching for repository operations.

The cache identity is exactly what the operation's answer depends on::

    repository + commit_sha + operation + file_path + content_hash + arguments

``commit_sha`` pins a git checkout; ``content_hash`` covers the working tree
(uncommitted edits, or a repository staged from a zip with no git history at
all), so a stale entry cannot be served after a file changes.

Two implementations ship: :class:`NullCache` (default — correct, never stale)
and :class:`JsonFileCache` (one JSON document per key on local disk). Both
satisfy :class:`OperationCache`, which is the seam a production backend
(Redis, S3, Postgres) would implement later without touching the executor.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Protocol

from operations.schemas import OperationResult

logger = logging.getLogger("zdocs.operations.cache")


def cache_key(
    *,
    repository: str,
    commit_sha: str | None,
    operation: str,
    file_path: str | None,
    content_hash: str,
    arguments: dict[str, Any],
) -> str:
    """Stable SHA-256 identity for one operation invocation.

    ``arguments`` is serialized with sorted keys so equivalent requests that
    differ only in dict ordering share a cache entry.
    """
    payload = json.dumps(
        {
            "repository": repository,
            "commit_sha": commit_sha or "",
            "operation": operation,
            "file_path": file_path or "",
            "content_hash": content_hash,
            "arguments": arguments,
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def content_fingerprint(root: Path, relative_paths: list[str]) -> str:
    """Hash of ``(path, size, mtime_ns)`` for the given repository files.

    Cheap (no file contents are read) and sufficient: any edit changes size or
    mtime, so the fingerprint changes with it.
    """
    digest = hashlib.sha256()
    for relative in sorted(relative_paths):
        digest.update(relative.encode("utf-8"))
        try:
            stat = (root / relative).stat()
        except OSError:
            digest.update(b"\x00missing")
            continue
        digest.update(f":{stat.st_size}:{stat.st_mtime_ns}".encode("ascii"))
    return digest.hexdigest()


def file_fingerprint(path: Path) -> str:
    """Content hash of a single file (``""`` when it cannot be read)."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


class OperationCache(Protocol):
    """Storage contract for cached operation results."""

    def get(self, key: str) -> OperationResult | None:
        """Return a cached result, or ``None`` on a miss."""

    def set(self, key: str, result: OperationResult) -> None:
        """Store ``result`` under ``key``."""

    def clear(self) -> None:
        """Drop everything."""


class NullCache:
    """No-op cache. The default: always correct, never stale."""

    def get(self, key: str) -> OperationResult | None:
        return None

    def set(self, key: str, result: OperationResult) -> None:
        return None

    def clear(self) -> None:
        return None


class JsonFileCache:
    """Local JSON cache, one file per key.

    Intended for a single-process development deployment. Entries expire after
    ``ttl_seconds`` and the directory is trimmed to ``max_entries`` (oldest
    first) on write.
    """

    def __init__(
        self,
        directory: str | Path,
        *,
        ttl_seconds: float = 24 * 3600,
        max_entries: int = 500,
    ) -> None:
        self.directory = Path(directory)
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def get(self, key: str) -> OperationResult | None:
        path = self._path(key)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            document = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Discarding unreadable cache entry: %s", path.name)
            path.unlink(missing_ok=True)
            return None
        if time.time() - float(document.get("stored_at", 0)) > self.ttl_seconds:
            path.unlink(missing_ok=True)
            return None
        try:
            return OperationResult.model_validate(document["result"])
        except (KeyError, ValueError):
            path.unlink(missing_ok=True)
            return None

    def set(self, key: str, result: OperationResult) -> None:
        document = {
            "stored_at": time.time(),
            "result": result.model_dump(mode="json"),
        }
        path = self._path(key)
        # Write-then-rename so a crash mid-write cannot leave a torn entry.
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(document), encoding="utf-8")
        os.replace(temporary, path)
        self._evict()

    def clear(self) -> None:
        for entry in self.directory.glob("*.json"):
            entry.unlink(missing_ok=True)

    def _evict(self) -> None:
        entries = sorted(
            self.directory.glob("*.json"), key=lambda item: item.stat().st_mtime
        )
        for entry in entries[: max(0, len(entries) - self.max_entries)]:
            entry.unlink(missing_ok=True)


__all__ = [
    "JsonFileCache",
    "NullCache",
    "OperationCache",
    "cache_key",
    "content_fingerprint",
    "file_fingerprint",
]
